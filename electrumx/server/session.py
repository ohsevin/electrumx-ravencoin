# Copyright (c) 2016-2018, Neil Booth
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''Classes for local RPC server and remote client TCP/SSL servers.'''

import codecs
import itertools
import json
import math
import os
import ssl
import time
from collections import defaultdict
from functools import partial
from ipaddress import IPv4Address, IPv6Address

import attr
import pylru
from aiorpcx import (
    RPCSession, JSONRPCAutoDetect, JSONRPCConnection, serve_rs, serve_ws, NewlineFramer,
    TaskGroup, handler_invocation, RPCError, Request, sleep, Event, ReplyAndDisconnect,
    timeout_after
)

import electrumx

from electrumx.lib.merkle import MerkleCache
from electrumx.lib.text import sessions_lines
from electrumx.lib import util
from electrumx.lib.hash import (sha256, hash_to_hex_str, hex_str_to_hash, HASHX_LEN, Base58Error,
                                double_sha256)

from electrumx.server.daemon import DaemonError
from electrumx.server.peers import PeerManager

BAD_REQUEST = 1
DAEMON_ERROR = 2


def scripthash_to_hashX(scripthash):
    try:
        bin_hash = hex_str_to_hash(scripthash)
        if len(bin_hash) == 32:
            return bin_hash[:HASHX_LEN]
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{scripthash} is not a valid script hash')


def non_negative_integer(value):
    '''Return param value it is or can be converted to a non-negative
    integer, otherwise raise an RPCError.'''
    try:
        value = int(value)
        if value >= 0:
            return value
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST,
                   f'{value} should be a non-negative integer')


def assert_boolean(value):
    '''Return param value it is boolean otherwise raise an RPCError.'''
    if value in (False, True):
        return value
    raise RPCError(BAD_REQUEST, f'{value} should be a boolean value')


def assert_tx_hash(value):
    '''Raise an RPCError if the value is not a valid hexadecimal transaction hash.

    If it is valid, return it as 32-byte binary hash.
    '''
    try:
        raw_hash = hex_str_to_hash(value)
        if len(raw_hash) == 32:
            return raw_hash
    except (ValueError, TypeError):
        pass
    raise RPCError(BAD_REQUEST, f'{value} should be a transaction hash')


@attr.s(slots=True)
class SessionGroup:
    name = attr.ib()
    weight = attr.ib()
    sessions = attr.ib()
    retained_cost = attr.ib()

    def session_cost(self):
        return sum(session.cost for session in self.sessions)

    def cost(self):
        return self.retained_cost + self.session_cost()


@attr.s(slots=True)
class SessionReferences:
    # All attributes are sets but groups is a list
    sessions = attr.ib()
    groups = attr.ib()
    specials = attr.ib()    # Lower-case strings
    unknown = attr.ib()     # Strings


class SessionManager:
    '''Holds global state about all sessions.'''

    def __init__(self, env, db, bp, daemon, mempool, shutdown_event):
        env.max_send = max(350000, env.max_send)
        self.env = env
        self.db = db
        self.bp = bp
        self.daemon = daemon
        self.mempool = mempool
        self.peer_mgr = PeerManager(env, db)
        self.shutdown_event = shutdown_event
        self.logger = util.class_logger(__name__, self.__class__.__name__)
        self.servers = {}           # service->server
        self.sessions = {}          # session->iterable of its SessionGroups
        self.session_groups = {}    # group name->SessionGroup instance
        self.txs_sent = 0
        # Would use monotonic time, but aiorpcx sessions use Unix time:
        self.start_time = time.time()
        self._method_counts = defaultdict(int)
        self._reorg_count = 0
        self._history_cache = pylru.lrucache(1000)
        self._history_lookups = 0
        self._history_hits = 0
        self._tx_hashes_cache = pylru.lrucache(1000)
        self._tx_hashes_lookups = 0
        self._tx_hashes_hits = 0
        # Really a MerkleCache cache
        self._merkle_cache = pylru.lrucache(1000)
        self._merkle_lookups = 0
        self._merkle_hits = 0
        self.notified_height = None
        self.hsub_results = None
        self._sslc = None
        # Event triggered when electrumx is listening for incoming requests.
        self.server_listening = Event()
        self.session_event = Event()

        # Set up the RPC request handlers
        cmds = ('add_peer daemon_url disconnect getinfo groups log peers '
                'query reorg sessions stop'.split())
        self.rpc_request_handlers = {cmd: getattr(self, 'rpc_' + cmd)
                                     for cmd in cmds}

    def _ssl_context(self):
        if self._sslc is None:
            self._sslc = ssl.SSLContext(ssl.PROTOCOL_TLS)
            self._sslc.load_cert_chain(self.env.ssl_certfile, keyfile=self.env.ssl_keyfile)
        return self._sslc

    async def _start_servers(self, services):
        for service in services:
            kind = service.protocol.upper()
            if service.protocol in self.env.SSL_PROTOCOLS:
                sslc = self._ssl_context()
            else:
                sslc = None
            if service.protocol == 'rpc':
                session_class = LocalRPC
            else:
                session_class = self.env.coin.SESSIONCLS
            if service.protocol in ('ws', 'wss'):
                serve = serve_ws
            else:
                serve = serve_rs
            # FIXME: pass the service not the kind
            session_factory = partial(session_class, self, self.db, self.mempool,
                                      self.peer_mgr, kind)
            host = None if service.host == 'all_interfaces' else str(service.host)
            try:
                self.servers[service] = await serve(session_factory, host,
                                                    service.port, ssl=sslc, reuse_address=True)
            except OSError as e:    # don't suppress CancelledError
                self.logger.error(f'{kind} server failed to listen on {service.address}: {e}')
            else:
                self.logger.info(f'{kind} server listening on {service.address}')

    async def _start_external_servers(self):
        '''Start listening on TCP and SSL ports, but only if the respective
        port was given in the environment.
        '''
        await self._start_servers(service for service in self.env.services
                                  if service.protocol != 'rpc')
        self.server_listening.set()

    async def _stop_servers(self, services):
        '''Stop the servers of the given protocols.'''
        server_map = {service: self.servers.pop(service)
                      for service in set(services).intersection(self.servers)}
        # Close all before waiting
        for service, server in server_map.items():
            self.logger.info(f'closing down server for {service}')
            server.close()
        # No value in doing these concurrently
        for server in server_map.values():
            await server.wait_closed()

    async def _manage_servers(self):
        paused = False
        max_sessions = self.env.max_sessions
        low_watermark = max_sessions * 19 // 20
        while True:
            await self.session_event.wait()
            self.session_event.clear()
            if not paused and len(self.sessions) >= max_sessions:
                self.logger.info(f'maximum sessions {max_sessions:,d} '
                                 f'reached, stopping new connections until '
                                 f'count drops to {low_watermark:,d}')
                await self._stop_servers(service for service in self.servers
                                         if service.protocol != 'rpc')
                paused = True
            # Start listening for incoming connections if paused and
            # session count has fallen
            if paused and len(self.sessions) <= low_watermark:
                self.logger.info('resuming listening for incoming connections')
                await self._start_external_servers()
                paused = False

    async def _log_sessions(self):
        '''Periodically log sessions.'''
        log_interval = self.env.log_sessions
        if log_interval:
            while True:
                await sleep(log_interval)
                data = self._session_data(for_log=True)
                for line in sessions_lines(data):
                    self.logger.info(line)
                self.logger.info(json.dumps(self._get_info()))

    async def _disconnect_sessions(self, sessions, reason, *, force_after=1.0):
        if sessions:
            session_ids = ', '.join(str(session.session_id) for session in sessions)
            self.logger.info(f'{reason} session ids {session_ids}')
            async with TaskGroup() as group:
                for session in sessions:
                    await group.spawn(session.close(force_after=force_after))

    async def _clear_stale_sessions(self):
        '''Cut off sessions that haven't done anything for 10 minutes.'''
        while True:
            await sleep(60)
            stale_cutoff = time.time() - self.env.session_timeout
            stale_sessions = [session for session in self.sessions
                              if session.last_recv < stale_cutoff]
            await self._disconnect_sessions(stale_sessions, 'closing stale')
            del stale_sessions

    async def _handle_chain_reorgs(self):
        '''Clear caches on chain reorgs.'''
        while True:
            await self.bp.backed_up_event.wait()
            self.logger.info('reorg signalled; clearing tx_hashes and merkle caches')
            self._reorg_count += 1
            self._tx_hashes_cache.clear()
            self._merkle_cache.clear()

    async def _recalc_concurrency(self):
        '''Periodically recalculate session concurrency.'''
        session_class = self.env.coin.SESSIONCLS
        period = 300
        while True:
            await sleep(period)
            hard_limit = session_class.cost_hard_limit

            # Reduce retained group cost
            refund = period * hard_limit / 5000
            dead_groups = []
            for group in self.session_groups.values():
                group.retained_cost = max(0.0, group.retained_cost - refund)
                if group.retained_cost == 0 and not group.sessions:
                    dead_groups.append(group)
            # Remove dead groups
            for group in dead_groups:
                self.session_groups.pop(group.name)

            # Recalc concurrency for sessions where cost is changing gradually, and update
            # cost_decay_per_sec.
            for session in self.sessions:
                # Subs have an on-going cost so decay more slowly with more subs
                session.cost_decay_per_sec = hard_limit / (10000 + 5 * session.sub_count())
                session.recalc_concurrency()

    def _get_info(self):
        '''A summary of server state.'''
        cache_fmt = '{:,d} lookups {:,d} hits {:,d} entries'
        sessions = self.sessions
        return {
            'coin': self.env.coin.__name__,
            'daemon': self.daemon.logged_url(),
            'daemon height': self.daemon.cached_height(),
            'db height': self.db.db_height,
            'db_flush_count': self.db.history.flush_count,
            'groups': len(self.session_groups),
            'history cache': cache_fmt.format(
                self._history_lookups, self._history_hits, len(self._history_cache)),
            'merkle cache': cache_fmt.format(
                self._merkle_lookups, self._merkle_hits, len(self._merkle_cache)),
            'pid': os.getpid(),
            'peers': self.peer_mgr.info(),
            'request counts': self._method_counts,
            'request total': sum(self._method_counts.values()),
            'sessions': {
                'count': len(sessions),
                'count with subs': sum(len(getattr(s, 'hashX_subs', ())) > 0 for s in sessions),
                'errors': sum(s.errors for s in sessions),
                'logged': len([s for s in sessions if s.log_me]),
                'pending requests': sum(s.unanswered_request_count() for s in sessions),
                'subs': sum(s.sub_count() for s in sessions),
            },
            'tx hashes cache': cache_fmt.format(
                self._tx_hashes_lookups, self._tx_hashes_hits, len(self._tx_hashes_cache)),
            'txs sent': self.txs_sent,
            'uptime': util.formatted_time(time.time() - self.start_time),
            'version': electrumx.version,
        }

    def _session_data(self, for_log):
        '''Returned to the RPC 'sessions' call.'''
        now = time.time()
        sessions = sorted(self.sessions, key=lambda s: s.start_time)
        return [(session.session_id,
                 session.flags(),
                 session.remote_address_string(for_log=for_log),
                 session.client,
                 session.protocol_version_string(),
                 session.cost,
                 session.extra_cost(),
                 session.unanswered_request_count(),
                 session.txs_sent,
                 session.sub_count(),
                 session.recv_count, session.recv_size,
                 session.send_count, session.send_size,
                 now - session.start_time)
                for session in sessions]

    def _group_data(self):
        '''Returned to the RPC 'groups' call.'''
        result = []
        for name, group in self.session_groups.items():
            sessions = group.sessions
            result.append([name,
                           len(sessions),
                           group.session_cost(),
                           group.retained_cost,
                           sum(s.unanswered_request_count() for s in sessions),
                           sum(s.txs_sent for s in sessions),
                           sum(s.sub_count() for s in sessions),
                           sum(s.recv_count for s in sessions),
                           sum(s.recv_size for s in sessions),
                           sum(s.send_count for s in sessions),
                           sum(s.send_size for s in sessions),
                           ])
        return result

    async def _refresh_hsub_results(self, height):
        '''Refresh the cached header subscription responses to be for height,
        and record that as notified_height.
        '''
        # Paranoia: a reorg could race and leave db_height lower
        height = min(height, self.db.db_height)
        raw = await self.raw_header(height)
        self.hsub_results = {'hex': raw.hex(), 'height': height}
        self.notified_height = height

    def _session_references(self, items, special_strings):
        '''Return a SessionReferences object.'''
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise RPCError(BAD_REQUEST, 'expected a list of session IDs')

        sessions_by_id = {session.session_id: session for session in self.sessions}
        groups_by_name = self.session_groups

        sessions = set()
        groups = set()     # Names as groups are not hashable
        specials = set()
        unknown = set()

        for item in items:
            if item.isdigit():
                session = sessions_by_id.get(int(item))
                if session:
                    sessions.add(session)
                else:
                    unknown.add(item)
            else:
                lc_item = item.lower()
                if lc_item in special_strings:
                    specials.add(lc_item)
                else:
                    if lc_item in groups_by_name:
                        groups.add(lc_item)
                    else:
                        unknown.add(item)

        groups = [groups_by_name[group] for group in groups]
        return SessionReferences(sessions, groups, specials, unknown)

    # --- LocalRPC command handlers

    async def rpc_add_peer(self, real_name):
        '''Add a peer.

        real_name: "bch.electrumx.cash t50001 s50002" for example
        '''
        await self.peer_mgr.add_localRPC_peer(real_name)
        return "peer '{}' added".format(real_name)

    async def rpc_disconnect(self, session_ids):
        '''Disconnect sesssions.

        session_ids: array of session IDs
        '''
        refs = self._session_references(session_ids, {'all'})
        result = []

        if 'all' in refs.specials:
            sessions = self.sessions
            result.append('disconnecting all sessions')
        else:
            sessions = refs.sessions
            result.extend(f'disconnecting session {session.session_id}' for session in sessions)
            for group in refs.groups:
                result.append(f'disconnecting group {group.name}')
                sessions.update(group.sessions)
        result.extend(f'unknown: {item}' for item in refs.unknown)

        await self._disconnect_sessions(sessions, 'local RPC request to disconnect')
        return result

    async def rpc_log(self, session_ids):
        '''Toggle logging of sesssions.

        session_ids: array of session or group IDs, or 'all', 'none', 'new'
        '''
        refs = self._session_references(session_ids, {'all', 'none', 'new'})
        result = []

        def add_result(text, value):
            result.append(f'logging {text}' if value else f'not logging {text}')

        if 'all' in refs.specials:
            for session in self.sessions:
                session.log_me = True
            SessionBase.log_new = True
            result.append('logging all sessions')
        if 'none' in refs.specials:
            for session in self.sessions:
                session.log_me = False
            SessionBase.log_new = False
            result.append('logging no sessions')
        if 'new' in refs.specials:
            SessionBase.log_new = not SessionBase.log_new
            add_result('new sessions', SessionBase.log_new)

        sessions = refs.sessions
        for session in sessions:
            session.log_me = not session.log_me
            add_result(f'session {session.session_id}', session.log_me)
        for group in refs.groups:
            for session in group.sessions.difference(sessions):
                sessions.add(session)
                session.log_me = not session.log_me
                add_result(f'session {session.session_id}', session.log_me)

        result.extend(f'unknown: {item}' for item in refs.unknown)
        return result

    async def rpc_daemon_url(self, daemon_url):
        '''Replace the daemon URL.'''
        daemon_url = daemon_url or self.env.daemon_url
        try:
            self.daemon.set_url(daemon_url)
        except Exception as e:
            raise RPCError(BAD_REQUEST, f'an error occured: {e!r}') from None
        return f'now using daemon at {self.daemon.logged_url()}'

    async def rpc_stop(self):
        '''Shut down the server cleanly.'''
        self.shutdown_event.set()
        return 'stopping'

    async def rpc_getinfo(self):
        '''Return summary information about the server process.'''
        return self._get_info()

    async def rpc_groups(self):
        '''Return statistics about the session groups.'''
        return self._group_data()

    async def rpc_peers(self):
        '''Return a list of data about server peers.'''
        return self.peer_mgr.rpc_data()

    async def rpc_query(self, items, limit):
        '''Returns data about a script, address or name.'''
        coin = self.env.coin
        db = self.db
        lines = []

        def arg_to_hashX(arg):
            try:
                script = bytes.fromhex(arg)
                lines.append(f'Script: {arg}')
                return coin.hashX_from_script(script)
            except ValueError:
                pass

            try:
                hashX = coin.address_to_hashX(arg)
                lines.append(f'Address: {arg}')
                return hashX
            except Base58Error:
                pass

            try:
                script = coin.build_name_index_script(arg.encode("ascii"))
                hashX = coin.name_hashX_from_script(script)
                lines.append(f'Name: {arg}')
                return hashX
            except (AttributeError, UnicodeEncodeError):
                pass

            return None

        for arg in items:
            hashX = arg_to_hashX(arg)
            if not hashX:
                continue
            n = None
            history = await db.limited_history(hashX, limit=limit)
            for n, (tx_hash, height) in enumerate(history):
                lines.append(f'History #{n:,d}: height {height:,d} '
                             f'tx_hash {hash_to_hex_str(tx_hash)}')
            if n is None:
                lines.append('No history found')
            n = None
            utxos = await db.all_utxos(hashX)
            for n, utxo in enumerate(utxos, start=1):
                lines.append(f'UTXO #{n:,d}: tx_hash '
                             f'{hash_to_hex_str(utxo.tx_hash)} '
                             f'tx_pos {utxo.tx_pos:,d} height '
                             f'{utxo.height:,d} value {utxo.value:,d}')
                if n == limit:
                    break
            if n is None:
                lines.append('No UTXOs found')

            balance = sum(utxo.value for utxo in utxos)
            lines.append(f'Balance: {coin.decimal_value(balance):,f} '
                         f'{coin.SHORTNAME}')

        return lines

    async def rpc_sessions(self):
        '''Return statistics about connected sessions.'''
        return self._session_data(for_log=False)

    async def rpc_reorg(self, count):
        '''Force a reorg of the given number of blocks.

        count: number of blocks to reorg
        '''
        count = non_negative_integer(count)
        if not self.bp.force_chain_reorg(count):
            raise RPCError(BAD_REQUEST, 'still catching up with daemon')
        return f'scheduled a reorg of {count:,d} blocks'

    # --- External Interface

    async def serve(self, notifications, event):
        '''Start the RPC server if enabled.  When the event is triggered,
        start TCP and SSL servers.'''
        try:
            await self._start_servers(service for service in self.env.services
                                      if service.protocol == 'rpc')
            await event.wait()

            session_class = self.env.coin.SESSIONCLS
            session_class.cost_soft_limit = self.env.cost_soft_limit
            session_class.cost_hard_limit = self.env.cost_hard_limit
            session_class.cost_decay_per_sec = session_class.cost_hard_limit / 10000
            session_class.bw_cost_per_byte = 1.0 / self.env.bw_unit_cost
            session_class.cost_sleep = self.env.request_sleep / 1000
            session_class.initial_concurrent = self.env.initial_concurrent
            session_class.processing_timeout = self.env.request_timeout

            self.logger.info(f'max session count: {self.env.max_sessions:,d}')
            self.logger.info(f'session timeout: {self.env.session_timeout:,d} seconds')
            self.logger.info(f'session cost hard limit {self.env.cost_hard_limit:,d}')
            self.logger.info(f'session cost soft limit {self.env.cost_soft_limit:,d}')
            self.logger.info(f'bandwidth unit cost {self.env.bw_unit_cost:,d}')
            self.logger.info(f'request sleep {self.env.request_sleep:,d}ms')
            self.logger.info(f'request timeout {self.env.request_timeout:,d}s')
            self.logger.info(f'initial concurrent {self.env.initial_concurrent:,d}')

            self.logger.info(f'max response size {self.env.max_send:,d} bytes')
            if self.env.drop_client is not None:
                self.logger.info('drop clients matching: {}'
                                 .format(self.env.drop_client.pattern))
            for service in self.env.report_services:
                self.logger.info(f'advertising service {service}')
            # Start notifications; initialize hsub_results
            await notifications.start(self.db.db_height, self._notify_sessions)
            await self._start_external_servers()
            # Peer discovery should start after the external servers
            # because we connect to ourself
            async with TaskGroup() as group:
                await group.spawn(self.peer_mgr.discover_peers())
                await group.spawn(self._clear_stale_sessions())
                await group.spawn(self._handle_chain_reorgs())
                await group.spawn(self._recalc_concurrency())
                await group.spawn(self._log_sessions())
                await group.spawn(self._manage_servers())

                async for task in group:
                    if not task.cancelled():
                        task.result()

        finally:
            # Close servers then sessions
            self.logger.info('stopping servers')
            await self._stop_servers(self.servers.keys())
            self.logger.info('closing connections...')
            async with TaskGroup() as group:
                for session in list(self.sessions):
                    await group.spawn(session.close(force_after=1))
            self.logger.info('connections closed')

            # Fully stop the server
            raise

    def extra_cost(self, session):
        # Note there is no guarantee that session is still in self.sessions.  Example traceback:
        # notify_sessions->notify->address_status->bump_cost->recalc_concurrency->extra_cost
        # during which there are many places the sesssion could be removed
        groups = self.sessions.get(session)
        if groups is None:
            return 0
        return sum((group.cost() - session.cost) * group.weight for group in groups)

    async def _merkle_branch(self, height, tx_hashes, tx_pos, tsc_format=False):
        tx_hash_count = len(tx_hashes)
        cost = tx_hash_count

        if tx_hash_count >= 200:
            self._merkle_lookups += 1
            merkle_cache = self._merkle_cache.get(height)
            if merkle_cache:
                self._merkle_hits += 1
                cost = 10 * math.sqrt(tx_hash_count)
            else:
                async def tx_hashes_func(start, count):
                    return tx_hashes[start: start + count]

                merkle_cache = MerkleCache(self.db.merkle, tx_hashes_func)
                self._merkle_cache[height] = merkle_cache
                await merkle_cache.initialize(len(tx_hashes))
            branch, root = await merkle_cache.branch_and_root(tx_hash_count, tx_pos,
                                                              tsc_format=tsc_format)
        else:
            branch, root = self.db.merkle.branch_and_root(tx_hashes, tx_pos,
                                                          tsc_format=tsc_format)

        if tsc_format:
            def converter(_hash):
                if _hash == b"*":
                    return _hash.decode()
                else:
                    return hash_to_hex_str(_hash)
            branch = [converter(hash) for hash in branch]
        else:
            branch = [hash_to_hex_str(hash) for hash in branch]
        return branch, root, cost / 2500

    async def merkle_branch_for_tx_hash(self, height, tx_hash):
        '''Return a triple (branch, tx_pos, cost).'''
        tx_hashes, tx_hashes_cost = await self.tx_hashes_at_blockheight(height)
        try:
            tx_pos = tx_hashes.index(tx_hash)
        except ValueError:
            raise RPCError(
                BAD_REQUEST, f'tx {hash_to_hex_str(tx_hash)} not in block at height {height:,d}'
            ) from None
        branch, _root, merkle_cost = await self._merkle_branch(height, tx_hashes, tx_pos)
        return branch, tx_pos, tx_hashes_cost + merkle_cost

    async def tsc_merkle_proof_for_tx_hash(self, height, tx_hash, txid_or_tx='txid',
                                           target_type='block_hash'):
        '''Return a pair (tsc_proof, cost) where tsc_proof is a dictionary with fields:
            index - the position of the transaction
            txOrId - either "txid" or "tx"
            target - either "block_hash", "block_header" or "merkle_root"
            nodes - the nodes in the merkle branch excluding the "target"'''

        async def get_target(target_type):
            try:
                cost = 0.25
                raw_header = await self.raw_header(height)
                root_from_header = raw_header[36:36 + 32]
                if target_type == "block_header":
                    target = raw_header.hex()
                elif target_type == "merkle_root":
                    target = hash_to_hex_str(root_from_header)
                else:  # target == block hash
                    target = hash_to_hex_str(double_sha256(raw_header))
            except ValueError:
                raise RPCError(BAD_REQUEST, f'block header at height {height:,d} not found') \
                    from None
            return target, root_from_header, cost

        def get_tx_position(tx_hash):
            try:
                tx_pos = tx_hashes.index(tx_hash)
            except ValueError:
                raise RPCError(BAD_REQUEST, f'tx {hash_to_hex_str(tx_hash)} not in block at height '
                                            f'{height:,d}') from None
            return tx_pos

        async def get_txid_or_tx_field(tx_hash):
            txid = hash_to_hex_str(tx_hash)
            if txid_or_tx == "tx":
                rawtx = await self.daemon_request('getrawtransaction', txid, False)
                cost = 1.0
                txid_or_tx_field = rawtx
            else:
                cost = 0.0
                txid_or_tx_field = txid
            return txid_or_tx_field, cost

        tsc_proof = {}
        tx_hashes, tx_hashes_cost = await self.tx_hashes_at_blockheight(height)
        tx_pos = get_tx_position(tx_hash)
        branch, root, merkle_cost = await self._merkle_branch(height, tx_hashes, tx_pos,
                                                              tsc_format=True)

        target, root_from_header, header_cost = await get_target(target_type)
        # sanity check
        if root != root_from_header:
            raise RPCError(BAD_REQUEST, 'db error. Merkle root from cached block header does not '
                                        'match the derived merkle root') from None

        txid_or_tx_field, tx_fetch_cost = await get_txid_or_tx_field(tx_hash)

        tsc_proof['index'] = tx_pos
        tsc_proof['txid_or_tx'] = txid_or_tx_field
        tsc_proof['target'] = target
        tsc_proof['nodes'] = branch
        return tsc_proof, tx_hashes_cost + merkle_cost + tx_fetch_cost + header_cost

    async def merkle_branch_for_tx_pos(self, height, tx_pos):
        '''Return a triple (branch, tx_hash_hex, cost).'''
        tx_hashes, tx_hashes_cost = await self.tx_hashes_at_blockheight(height)
        try:
            tx_hash = tx_hashes[tx_pos]
        except IndexError:
            raise RPCError(
                BAD_REQUEST, f'no tx at position {tx_pos:,d} in block at height {height:,d}'
            ) from None
        branch, _root, merkle_cost = await self._merkle_branch(height, tx_hashes, tx_pos)
        return branch, hash_to_hex_str(tx_hash), tx_hashes_cost + merkle_cost

    async def tx_hashes_at_blockheight(self, height):
        '''Returns a pair (tx_hashes, cost).

        tx_hashes is an ordered list of binary hashes, cost is an estimated cost of
        getting the hashes; cheaper if in-cache.  Raises RPCError.
        '''
        self._tx_hashes_lookups += 1
        tx_hashes = self._tx_hashes_cache.get(height)
        if tx_hashes:
            self._tx_hashes_hits += 1
            return tx_hashes, 0.1

        # Ensure the tx_hashes are fresh before placing in the cache
        while True:
            reorg_count = self._reorg_count
            try:
                tx_hashes = await self.db.tx_hashes_at_blockheight(height)
            except self.db.DBError as e:
                raise RPCError(BAD_REQUEST, f'db error: {e!r}') from None
            if reorg_count == self._reorg_count:
                break

        self._tx_hashes_cache[height] = tx_hashes

        return tx_hashes, 0.25 + len(tx_hashes) * 0.0001

    def session_count(self):
        '''The number of connections that we've sent something to.'''
        return len(self.sessions)

    async def daemon_request(self, method, *args):
        '''Catch a DaemonError and convert it to an RPCError.'''
        try:
            return await getattr(self.daemon, method)(*args)
        except DaemonError as e:
            raise RPCError(DAEMON_ERROR, f'daemon error: {e!r}') from None

    async def raw_header(self, height):
        '''Return the binary header at the given height.'''
        try:
            return await self.db.raw_header(height)
        except IndexError:
            raise RPCError(BAD_REQUEST, f'height {height:,d} '
                           'out of range') from None

    async def broadcast_transaction(self, raw_tx):
        hex_hash = await self.daemon.broadcast_transaction(raw_tx)
        self.txs_sent += 1
        return hex_hash

    async def limited_history(self, hashX):
        '''Returns a pair (history, cost).

        History is a sorted list of (tx_hash, height) tuples, or an RPCError.'''
        # History DoS limit.  Each element of history is about 99 bytes when encoded
        # as JSON.
        limit = self.env.max_send // 99
        cost = 0.1
        self._history_lookups += 1
        try:
            result = self._history_cache[hashX]
            self._history_hits += 1
        except KeyError:
            result = await self.db.limited_history(hashX, limit=limit)
            cost += 0.1 + len(result) * 0.001
            if len(result) >= limit:
                result = RPCError(BAD_REQUEST, 'history too large', cost=cost)
            self._history_cache[hashX] = result

        if isinstance(result, Exception):
            raise result
        return result, cost

    async def _notify_sessions(self, height, touched, assets):
        '''Notify sessions about height changes and touched addresses.'''
        height_changed = height != self.notified_height
        if height_changed:
            await self._refresh_hsub_results(height)
            # Invalidate our history cache for touched hashXs
            cache = self._history_cache
            for hashX in set(cache).intersection(touched):
                del cache[hashX]

        async with TaskGroup() as group:
            for session in self.sessions:
                await group.spawn(session.notify, touched, height_changed, assets)

    def _ip_addr_group_name(self, session):
        host = session.remote_address().host
        if isinstance(host, IPv4Address):
            if host.is_private:  # exempt private addresses
                return None
            return '.'.join(str(host).split('.')[:3])  # /24
        if isinstance(host, IPv6Address):
            if host.is_private:
                return None
            return ':'.join(host.exploded.split(':')[:3])  # /48
        return 'unknown_addr'

    def _timeslice_name(self, session):
        return f't{int(session.start_time - self.start_time) // 300}'

    def _session_group(self, name, weight):
        if name is None:
            return None
        group = self.session_groups.get(name)
        if not group:
            group = SessionGroup(name, weight, set(), 0)
            self.session_groups[name] = group
        return group

    def add_session(self, session):
        self.session_event.set()
        # Return the session groups
        groups = (
            self._session_group(self._timeslice_name(session), 0.03),
            self._session_group(self._ip_addr_group_name(session), 1.0),
        )
        groups = [group for group in groups if group is not None]
        self.sessions[session] = groups
        for group in groups:
            group.sessions.add(session)

    def remove_session(self, session):
        '''Remove a session from our sessions list if there.'''
        self.session_event.set()
        groups = self.sessions.pop(session)
        for group in groups:
            group.retained_cost += session.cost
            group.sessions.remove(session)


class SessionBase(RPCSession):
    '''Base class of ElectrumX JSON sessions.

    Each session runs its tasks in asynchronous parallelism with other
    sessions.
    '''

    MAX_CHUNK_SIZE = 2016
    session_counter = itertools.count()
    log_new = False

    def __init__(self, session_mgr, db, mempool, peer_mgr, kind, transport):
        connection = JSONRPCConnection(JSONRPCAutoDetect)
        super().__init__(transport, connection=connection)
        self.session_mgr = session_mgr
        self.db = db
        self.mempool = mempool
        self.peer_mgr = peer_mgr
        self.kind = kind  # 'RPC', 'TCP' etc.
        self.env = session_mgr.env
        self.coin = self.env.coin
        self.client = 'unknown'
        self.anon_logs = self.env.anon_logs
        self.txs_sent = 0
        self.log_me = SessionBase.log_new
        self.session_id = None
        self.daemon_request = self.session_mgr.daemon_request
        self.session_id = next(self.session_counter)
        context = {'conn_id': f'{self.session_id}'}
        logger = util.class_logger(__name__, self.__class__.__name__)
        self.logger = util.ConnectionLogger(logger, context)
        self.logger.info(f'{self.kind} {self.remote_address_string()}, '
                         f'{self.session_mgr.session_count():,d} total')
        self.session_mgr.add_session(self)
        self.recalc_concurrency()  # must be called after session_mgr.add_session
        self.request_handlers = {}

    async def notify(self, touched, height_changed, assets):
        pass

    def default_framer(self):
        return NewlineFramer(max_size=self.env.max_recv)

    def remote_address_string(self, *, for_log=True):
        '''Returns the peer's IP address and port as a human-readable
        string, respecting anon logs if the output is for a log.'''
        if for_log and self.anon_logs:
            return 'xx.xx.xx.xx:xx'
        return str(self.remote_address())

    def flags(self):
        '''Status flags.'''
        status = self.kind[0]
        if self.is_closing():
            status += 'C'
        if self.log_me:
            status += 'L'
        status += str(self._incoming_concurrency.max_concurrent)
        return status

    async def connection_lost(self):
        '''Handle client disconnection.'''
        await super().connection_lost()
        self.session_mgr.remove_session(self)
        msg = ''
        if self._incoming_concurrency.max_concurrent < self.initial_concurrent * 0.8:
            msg += ' whilst throttled'
        if self.send_size >= 1_000_000:
            msg += f'.  Sent {self.send_size:,d} bytes in {self.send_count:,d} messages'
        if msg:
            msg = 'disconnected' + msg
            self.logger.info(msg)

    def sub_count(self):
        return 0

    async def handle_request(self, request):
        '''Handle an incoming request.  ElectrumX doesn't receive
        notifications from client sessions.
        '''
        if isinstance(request, Request):
            handler = self.request_handlers.get(request.method)
        else:
            handler = None
        method = 'invalid method' if handler is None else request.method
        self.session_mgr._method_counts[method] += 1
        coro = handler_invocation(handler, request)()
        return await coro


def check_asset_len(name):
    if len(name) > 32:
        raise RPCError(
            BAD_REQUEST, f'asset name greater than 32 characters'
        ) from None


class ElectrumX(SessionBase):
    '''A TCP server that handles incoming Electrum connections.'''

    PROTOCOL_MIN = (1, 4)
    PROTOCOL_MAX = (1, 9)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subscribe_headers = False
        self.connection.max_response_size = self.env.max_send
        self.hashX_subs = {}
        self.asset_subs = set()
        self.sv_seen = False
        self.mempool_statuses = {}
        self.set_request_handlers(self.PROTOCOL_MIN)
        self.is_peer = False
        self.cost = 5.0   # Connection cost

    @classmethod
    def protocol_min_max_strings(cls):
        return [util.version_string(ver)
                for ver in (cls.PROTOCOL_MIN, cls.PROTOCOL_MAX)]

    @classmethod
    def server_features(cls, env):
        '''Return the server features dictionary.'''
        hosts_dict = {}
        for service in env.report_services:
            port_dict = hosts_dict.setdefault(str(service.host), {})
            if service.protocol not in port_dict:
                port_dict[f'{service.protocol}_port'] = service.port

        min_str, max_str = cls.protocol_min_max_strings()
        return {
            'hosts': hosts_dict,
            'pruning': None,
            'server_version': electrumx.version,
            'protocol_min': min_str,
            'protocol_max': max_str,
            'genesis_hash': env.coin.GENESIS_HASH,
            'hash_function': 'sha256',
            'services': [str(service) for service in env.report_services],
        }

    async def server_features_async(self):
        self.bump_cost(0.2)
        return self.server_features(self.env)

    @classmethod
    def server_version_args(cls):
        '''The arguments to a server.version RPC call to a peer.'''
        return [electrumx.version, cls.protocol_min_max_strings()]

    def protocol_version_string(self):
        return util.version_string(self.protocol_tuple)

    def extra_cost(self):
        return self.session_mgr.extra_cost(self)

    def on_disconnect_due_to_excessive_session_cost(self):
        ip_addr = self.remote_address().host
        groups = self.session_mgr.sessions[self]
        group_names = [group.name for group in groups]
        self.logger.info(f"closing session over res usage. ip: {ip_addr}. groups: {group_names}")

    def sub_count(self):
        return len(self.hashX_subs)

    def unsubscribe_hashX(self, hashX):
        self.mempool_statuses.pop(hashX, None)
        return self.hashX_subs.pop(hashX, None)

    async def notify(self, touched, height_changed, assets):
        '''Wrap _notify_inner; websockets raises exceptions for unclear reasons.'''
        try:
            async with timeout_after(30):
                await self._notify_inner(touched, height_changed, assets)
        except Exception:   # pylint:disable=W0703
            self.logger.exception('unexpected exception notifying client')

    async def _notify_inner(self, touched, height_changed, assets):
        '''Notify the client about changes to touched addresses (from mempool
        updates or new blocks) and height.
        '''
        if height_changed and self.subscribe_headers:
            args = (await self.subscribe_headers_result(), )
            await self.send_notification('blockchain.headers.subscribe', args)

        touched_assets = assets.intersection(self.asset_subs)
        if touched_assets:
            method = 'blockchain.asset.subscribe'
            for asset in touched_assets:
                status = await self.asset_status(asset)
                await self.send_notification(method, (asset, status))
            es = '' if len(touched_assets) == 1 else 's'
            self.logger.info(f'notified of {len(touched_assets):,d} reissued asset{es}')

        touched = touched.intersection(self.hashX_subs)
        if touched or (height_changed and self.mempool_statuses):
            changed = {}

            for hashX in touched:
                alias = self.hashX_subs.get(hashX)
                if alias:
                    status = await self.subscription_address_status(hashX)
                    changed[alias] = status

            # Check mempool hashXs - the status is a function of the confirmed state of
            # other transactions.
            mempool_statuses = self.mempool_statuses.copy()
            for hashX, old_status in mempool_statuses.items():
                alias = self.hashX_subs.get(hashX)
                if alias:
                    status = await self.subscription_address_status(hashX)
                    if status != old_status:
                        changed[alias] = status

            method = 'blockchain.scripthash.subscribe'
            for alias, status in changed.items():
                await self.send_notification(method, (alias, status))

            if changed:
                es = '' if len(changed) == 1 else 'es'
                self.logger.info(f'notified of {len(changed):,d} address{es}')

    async def subscribe_headers_result(self):
        '''The result of a header subscription or notification.'''
        return self.session_mgr.hsub_results

    async def headers_subscribe(self):
        '''Subscribe to get raw headers of new blocks.'''
        self.subscribe_headers = True
        self.bump_cost(0.25)
        return await self.subscribe_headers_result()

    async def add_peer(self, features):
        '''Add a peer (but only if the peer resolves to the source).'''
        self.is_peer = True
        self.bump_cost(100.0)
        return await self.peer_mgr.on_add_peer(features, self.remote_address())

    async def peers_subscribe(self):
        '''Return the server peers as a list of (ip, host, details) tuples.'''
        self.bump_cost(1.0)
        return self.peer_mgr.on_peers_subscribe(self.is_tor())

    async def asset_status(self, asset):
        asset_data = await self.session_mgr.db.lookup_asset_meta(asset.encode('ascii'))
        self.bump_cost(0.1 + len(asset_data) * 0.00002)
        ptuple = self.protocol_tuple
        if asset_data:
            sats = str(asset_data['sats_in_circulation']) if ptuple >= (1, 9) else ''
            div_amt = asset_data['divisions']
            reissuable = False if asset_data['reissuable'] == 0 else True
            has_ipfs = False if asset_data['has_ipfs'] == 0 else True

            h = ''.join([sats, str(div_amt), str(reissuable), str(has_ipfs)])
            if has_ipfs:
                h += asset_data['ipfs']

            status = sha256(h.encode('ascii')).hex()
        else:
            status = None

        return status

    async def address_status(self, hashX):
        '''Returns an address status.

        Status is a hex string, but must be None if there is no history.
        '''
        # Note history is ordered and mempool unordered in electrum-server
        # For mempool, height is -1 if it has unconfirmed inputs, otherwise 0
        db_history, cost = await self.session_mgr.limited_history(hashX)
        mempool = await self.mempool.transaction_summaries(hashX)

        status = ''.join(f'{hash_to_hex_str(tx_hash)}:'
                         f'{height:d}:'
                         for tx_hash, height in db_history)
        status += ''.join(f'{hash_to_hex_str(tx.hash)}:'
                          f'{-tx.has_unconfirmed_inputs:d}:'
                          for tx in mempool)

        # Add status hashing cost
        self.bump_cost(cost + 0.1 + len(status) * 0.00002)

        if status:
            status = sha256(status.encode()).hex()
        else:
            status = None

        if mempool:
            self.mempool_statuses[hashX] = status
        else:
            self.mempool_statuses.pop(hashX, None)

        return status

    async def subscription_address_status(self, hashX):
        '''As for address_status, but if it can't be calculated the subscription is
        discarded.'''
        try:
            return await self.address_status(hashX)
        except RPCError:
            self.unsubscribe_hashX(hashX)
            return None

    async def hashX_listassets(self, hashX):
        assets = await self.db.all_assets(hashX)
        assets = sorted(assets)
        assets.extend(await self.mempool.unordered_ASSETs(hashX))
        self.bump_cost(1.0 + len(assets) / 50)
        spends = await self.mempool.potential_spends(hashX)

        return [{'tx_hash': hash_to_hex_str(asset.tx_hash),
                 'tx_pos': asset.tx_pos,
                 'height': asset.height,
                 'name': asset.name,
                 'value': asset.value}
                for asset in assets
                if (asset.tx_hash, asset.tx_pos) not in spends]

    async def hashX_listunspent(self, hashX):
        '''Return the list of UTXOs of a script hash, including mempool
        effects.'''
        utxos = await self.db.all_utxos(hashX)
        utxos = sorted(utxos)
        utxos.extend(await self.mempool.unordered_UTXOs(hashX))
        self.bump_cost(1.0 + len(utxos) / 50)
        spends = await self.mempool.potential_spends(hashX)

        return [{'tx_hash': hash_to_hex_str(utxo.tx_hash),
                 'tx_pos': utxo.tx_pos,
                 'height': utxo.height, 'value': utxo.value}
                for utxo in utxos
                if (utxo.tx_hash, utxo.tx_pos) not in spends]

    async def hashX_subscribe(self, hashX, alias):
        # Store the subscription only after address_status succeeds
        result = await self.address_status(hashX)
        self.hashX_subs[hashX] = alias
        return result

    async def asset_subscribe(self, asset):
        if len(asset) > 31:
            raise RPCError(
                BAD_REQUEST, f'asset name greater than 31 characters'
            ) from None
        result = await self.asset_status(asset)
        self.asset_subs.add(asset)
        return result

    async def asset_unsubscribe(self, asset):
        if len(asset) > 31:
            raise RPCError(
                BAD_REQUEST, f'asset name greater than 31 characters'
            ) from None
        return self.asset_subs.discard(asset) is not None

    async def get_balance(self, hashX):
        utxos = await self.db.all_utxos(hashX)
        confirmed = sum(utxo.value for utxo in utxos)
        unconfirmed = await self.mempool.balance_delta(hashX)
        self.bump_cost(1.0 + len(utxos) / 50)
        return {'confirmed': confirmed, 'unconfirmed': unconfirmed}

    async def get_asset_balance(self, hashX):
        assets = await self.db.all_assets(hashX)
        confirmed = {}
        for asset in assets:
            if asset.name not in confirmed:
                confirmed[asset.name] = asset.value
            else:
                confirmed[asset.name] += asset.value
        unconfirmed = await self.mempool.asset_balance_delta(hashX)
        self.bump_cost(1.0 + len(assets) / 50)
        return {'confirmed': confirmed, 'unconfirmed': unconfirmed}


    async def scripthash_get_balance(self, scripthash):
        '''Return the confirmed and unconfirmed balance of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.get_balance(hashX)

    async def scripthash_get_asset_balance(self, scripthash):
        hashX = scripthash_to_hashX(scripthash)
        return await self.get_asset_balance(hashX)

    async def unconfirmed_history(self, hashX):
        # Note unconfirmed history is unordered in electrum-server
        # height is -1 if it has unconfirmed inputs, otherwise 0
        result = [{'tx_hash': hash_to_hex_str(tx.hash),
                   'height': -tx.has_unconfirmed_inputs,
                   'fee': tx.fee}
                  for tx in await self.mempool.transaction_summaries(hashX)]
        self.bump_cost(0.25 + len(result) / 50)
        return result

    async def confirmed_and_unconfirmed_history(self, hashX):
        # Note history is ordered but unconfirmed is unordered in e-s
        history, cost = await self.session_mgr.limited_history(hashX)
        self.bump_cost(cost)
        conf = [{'tx_hash': hash_to_hex_str(tx_hash), 'height': height}
                for tx_hash, height in history]
        return conf + await self.unconfirmed_history(hashX)

    async def scripthash_get_history(self, scripthash):
        '''Return the confirmed and unconfirmed history of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.confirmed_and_unconfirmed_history(hashX)

    async def scripthash_get_mempool(self, scripthash):
        '''Return the mempool transactions touching a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.unconfirmed_history(hashX)

    async def scripthash_listunspent(self, scripthash):
        '''Return the list of UTXOs of a scripthash.'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_listunspent(hashX)

    async def scripthash_listassets(self, scripthash):
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_listassets(hashX)

    async def scripthash_subscribe(self, scripthash):
        '''Subscribe to a script hash.

        scripthash: the SHA256 hash of the script to subscribe to'''
        hashX = scripthash_to_hashX(scripthash)
        return await self.hashX_subscribe(hashX, scripthash)

    async def scripthash_unsubscribe(self, scripthash):
        '''Unsubscribe from a script hash.'''
        self.bump_cost(0.1)
        hashX = scripthash_to_hashX(scripthash)
        return self.unsubscribe_hashX(hashX) is not None

    async def _merkle_proof(self, cp_height, height):
        max_height = self.db.db_height
        if not height <= cp_height <= max_height:
            raise RPCError(BAD_REQUEST,
                           f'require header height {height:,d} <= '
                           f'cp_height {cp_height:,d} <= '
                           f'chain height {max_height:,d}')
        branch, root = await self.db.header_branch_and_root(cp_height + 1,
                                                            height)
        return {
            'branch': [hash_to_hex_str(elt) for elt in branch],
            'root': hash_to_hex_str(root),
        }

    async def block_header(self, height, cp_height=0):
        '''Return a raw block header as a hexadecimal string, or as a
        dictionary with a merkle proof.'''
        height = non_negative_integer(height)
        cp_height = non_negative_integer(cp_height)
        raw_header_hex = (await self.session_mgr.raw_header(height)).hex()
        self.bump_cost(1.25 - (cp_height == 0))
        if cp_height == 0:
            return raw_header_hex
        result = {'header': raw_header_hex}
        result.update(await self._merkle_proof(cp_height, height))
        return result

    async def block_headers(self, start_height, count, cp_height=0):
        '''Return count concatenated block headers as hex for the main chain;
        starting at start_height.

        start_height and count must be non-negative integers.  At most
        MAX_CHUNK_SIZE headers will be returned.
        '''
        start_height = non_negative_integer(start_height)
        count = non_negative_integer(count)
        cp_height = non_negative_integer(cp_height)
        cost = count / 50

        max_size = self.MAX_CHUNK_SIZE
        count = min(count, max_size)
        headers, count = await self.db.read_headers(start_height, count)
        result = {'hex': headers.hex(), 'count': count, 'max': max_size}
        if count and cp_height:
            cost += 1.0
            last_height = start_height + count - 1
            result.update(await self._merkle_proof(cp_height, last_height))
        self.bump_cost(cost)
        return result

    def is_tor(self):
        '''Try to detect if the connection is to a tor hidden service we are
        running.'''
        proxy_address = self.peer_mgr.proxy_address()
        if not proxy_address:
            return False
        return self.remote_address().host == proxy_address.host

    async def replaced_banner(self, banner):
        network_info = await self.daemon_request('getnetworkinfo')
        ni_version = network_info['version']
        major, minor = divmod(ni_version, 1000000)
        minor, revision = divmod(minor, 10000)
        revision //= 100
        daemon_version = '{:d}.{:d}.{:d}'.format(major, minor, revision)
        for pair in [
                ('$SERVER_VERSION', electrumx.version_short),
                ('$SERVER_SUBVERSION', electrumx.version),
                ('$DAEMON_VERSION', daemon_version),
                ('$DAEMON_SUBVERSION', network_info['subversion']),
                ('$DONATION_ADDRESS', self.env.donation_address),
        ]:
            banner = banner.replace(*pair)
        return banner

    async def donation_address(self):
        '''Return the donation address as a string, empty if there is none.'''
        self.bump_cost(0.1)
        return self.env.donation_address

    async def banner(self):
        '''Return the server banner text.'''
        banner = f'You are connected to an {electrumx.version} server.'
        self.bump_cost(0.5)

        if self.is_tor():
            banner_file = self.env.tor_banner_file
        else:
            banner_file = self.env.banner_file
        if banner_file:
            try:
                with codecs.open(banner_file, 'r', 'utf-8') as f:
                    banner = f.read()
            except (OSError, UnicodeDecodeError) as e:
                self.logger.error(f'reading banner file {banner_file}: {e!r}')
            else:
                banner = await self.replaced_banner(banner)

        return banner

    async def relayfee(self):
        '''The minimum fee a low-priority tx must pay in order to be accepted
        to the daemon's memory pool.'''
        self.bump_cost(1.0)
        return 0.01

    async def estimatefee(self, _number):
        '''The estimated transaction fee per kilobyte to be paid for a
        transaction to be included within a certain number of blocks.

        number: the number of blocks
        mode: CONSERVATIVE or ECONOMICAL estimation mode
        '''
        self.bump_cost(2.0)
        return 0.01

    async def ping(self):
        '''Serves as a connection keep-alive mechanism and for the client to
        confirm the server is still responding.
        '''
        self.bump_cost(0.1)
        return None

    async def server_version(self, client_name='', protocol_version=None):
        '''Returns the server version as a string.

        client_name: a string identifying the client
        protocol_version: the protocol version spoken by the client
        '''
        self.bump_cost(0.5)
        if self.sv_seen:
            raise RPCError(BAD_REQUEST, 'server.version already sent')
        self.sv_seen = True

        if client_name:
            client_name = str(client_name)
            if self.env.drop_client is not None and \
                    self.env.drop_client.match(client_name):
                raise ReplyAndDisconnect(RPCError(
                    BAD_REQUEST, f'unsupported client: {client_name}'))
            self.client = client_name[:17]

        # Find the highest common protocol version.  Disconnect if
        # that protocol version in unsupported.
        ptuple, client_min = util.protocol_version(
            protocol_version, self.PROTOCOL_MIN, self.PROTOCOL_MAX)

        if ptuple is None:
            if client_min > self.PROTOCOL_MIN:
                self.logger.info(f'client requested future protocol version '
                                 f'{util.version_string(client_min)} '
                                 f'- is your software out of date?')
            raise ReplyAndDisconnect(RPCError(
                BAD_REQUEST, f'unsupported protocol version: {protocol_version}'))
        self.set_request_handlers(ptuple)

        return (electrumx.version, self.protocol_version_string())

    async def transaction_broadcast(self, raw_tx):
        '''Broadcast a raw transaction to the network.

        raw_tx: the raw transaction as a hexadecimal string'''
        self.bump_cost(0.25 + len(raw_tx) / 5000)
        # This returns errors as JSON RPC errors, as is natural
        try:
            hex_hash = await self.session_mgr.broadcast_transaction(raw_tx)
        except DaemonError as e:
            error, = e.args
            message = error['message']   # pylint:disable=E1126
            self.logger.info(f'error sending transaction: {message}')
            raise RPCError(BAD_REQUEST, 'the transaction was rejected by '
                           f'network rules.\n\n{message}\n[{raw_tx}]') from None
        else:
            self.txs_sent += 1
            self.logger.info(f'sent tx: {hex_hash}')
            return hex_hash

    async def transaction_get(self, tx_hash, verbose=False):
        '''Return the serialized raw transaction given its hash

        tx_hash: the transaction hash as a hexadecimal string
        verbose: passed on to the daemon
        '''
        assert_tx_hash(tx_hash)
        if verbose not in (True, False):
            raise RPCError(BAD_REQUEST, '"verbose" must be a boolean')

        self.bump_cost(1.0)
        return await self.daemon_request('getrawtransaction', tx_hash, verbose)

    async def transaction_merkle(self, tx_hash, height):
        '''Return the merkle branch to a confirmed transaction given its hash
        and height.

        tx_hash: the transaction hash as a hexadecimal string
        height: the height of the block it is in
        '''
        tx_hash = assert_tx_hash(tx_hash)
        height = non_negative_integer(height)

        branch, tx_pos, cost = await self.session_mgr.merkle_branch_for_tx_hash(
            height, tx_hash)
        self.bump_cost(cost)

        return {"block_height": height, "merkle": branch, "pos": tx_pos}

    async def transaction_tsc_merkle(self, tx_hash, height, txid_or_tx='txid',
                                     target_type='block_hash'):
        '''Return the TSC merkle proof in JSON format to a confirmed transaction given its hash.
        See: https://tsc.bitcoinassociation.net/standards/merkle-proof-standardised-format/.

        tx_hash: the transaction hash as a hexadecimal string
        include_tx: whether to include the full raw transaction in the response or txid.
        target: options include: ('merkle_root', 'block_header', 'block_hash', 'None')
        '''
        tx_hash = assert_tx_hash(tx_hash)
        height = non_negative_integer(height)

        tsc_proof, cost = await self.session_mgr.tsc_merkle_proof_for_tx_hash(
            height, tx_hash, txid_or_tx, target_type)
        self.bump_cost(cost)

        return {
            "index": tsc_proof['index'],
            "txOrId": tsc_proof['txid_or_tx'],
            "target": tsc_proof['target'],
            "nodes": tsc_proof['nodes'],  # "*" is used to represent duplicated hashes
            "targetType": target_type,
            "proofType": "branch",  # "tree" option is not supported by ElectrumX
            "composite": False  # composite option is not supported by ElectrumX
        }

    async def transaction_id_from_pos(self, height, tx_pos, merkle=False):
        '''Return the txid and optionally a merkle proof, given
        a block height and position in the block.
        '''
        tx_pos = non_negative_integer(tx_pos)
        height = non_negative_integer(height)
        if merkle not in (True, False):
            raise RPCError(BAD_REQUEST, '"merkle" must be a boolean')

        if merkle:
            branch, tx_hash, cost = await self.session_mgr.merkle_branch_for_tx_pos(
                height, tx_pos)
            self.bump_cost(cost)
            return {"tx_hash": tx_hash, "merkle": branch}
        else:
            tx_hashes, cost = await self.session_mgr.tx_hashes_at_blockheight(height)
            try:
                tx_hash = tx_hashes[tx_pos]
            except IndexError:
                raise RPCError(
                    BAD_REQUEST, f'no tx at position {tx_pos:,d} in block at height {height:,d}'
                ) from None
            self.bump_cost(cost)
            return hash_to_hex_str(tx_hash)

    async def asset_get_meta(self, name):
        self.bump_cost(1.0)
        return await self.db.lookup_asset_meta(name.encode('ascii'))

    async def get_assets_with_prefix(self, prefix):
        check_asset_len(prefix)
        ret = await self.db.get_assets_with_prefix(prefix.encode('ascii'))
        self.bump_cost(1.0 + len(ret) / 100)
        return ret

    async def get_messages(self, name):
        check_asset_len(name)
        ret = await self.db.lookup_messages(name.encode('ascii'))
        self.bump_cost(1.0 + len(ret) / 50)
        return ret

    async def is_qualified(self, h160: str, asset: str):
        check_asset_len(asset)
        self.bump_cost(1.0)
        return await self.db.is_qualified(asset.encode('ascii'), bytes.fromhex(h160))

    async def get_restricted_associations_current(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_associations_for_restricted_current(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_restricted_associations_history(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_associations_for_restricted_history(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_qualifier_associations_current(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_associations_for_qualifier_current(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_qualifier_associations_history(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_associations_for_qualifier_history(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_tags_for_h160_current(self, h160: str):
        ret = await self.db.get_tags_associated_with_h160_current(bytes.fromhex(h160))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_tags_for_h160_history(self, h160: str):
        ret = await self.db.get_tags_associated_with_h160_history(bytes.fromhex(h160))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_h160_for_asset_current(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_h160s_associated_with_asset_current(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def get_h160_for_asset_history(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_h160s_associated_with_asset_history(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def frozen_status_current(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_frozen_status_of_restricted_current(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def frozen_status_history(self, asset: str):
        check_asset_len(asset)

        ret = await self.db.get_frozen_status_of_restricted_history(asset.encode('ascii'))

        if not ret:
            self.bump_cost(1.0)
            return ret
        cost = 1.0 + len(ret) / 50
        self.bump_cost(cost)
        return ret

    async def compact_fee_histogram(self):
        self.bump_cost(1.0)
        return []

    async def get_session_stats(self):
        return {
            'our_cost': self.cost,
            'hard_limit': self.cost_hard_limit,
            'soft_limit': self.cost_soft_limit,
            'cost_decay_per_sec': self.cost_decay_per_sec,
            'bandwith_cost_per_byte': self.bw_cost_per_byte,
            'sleep': self.cost_sleep,
            'concurrent_requests': self._incoming_concurrency.max_concurrent,
            'send_size': self.send_size,
            'send_count': self.send_count,
            'receive_size': self.recv_size,
            'receive_count': self.recv_count
        }

    def set_request_handlers(self, ptuple):
        self.protocol_tuple = ptuple

        handlers = {
            'blockchain.block.header': self.block_header,
            'blockchain.block.headers': self.block_headers,
            'blockchain.estimatefee': self.estimatefee,
            'blockchain.headers.subscribe': self.headers_subscribe,
            'blockchain.relayfee': self.relayfee,
            'blockchain.scripthash.get_balance': self.scripthash_get_balance,
            'blockchain.scripthash.get_history': self.scripthash_get_history,
            'blockchain.scripthash.get_mempool': self.scripthash_get_mempool,
            'blockchain.scripthash.listunspent': self.scripthash_listunspent,
            'blockchain.scripthash.subscribe': self.scripthash_subscribe,
            'blockchain.transaction.broadcast': self.transaction_broadcast,
            'blockchain.transaction.get': self.transaction_get,
            'blockchain.transaction.get_merkle': self.transaction_merkle,
            'blockchain.transaction.get_tsc_merkle': self.transaction_tsc_merkle,
            'blockchain.transaction.id_from_pos': self.transaction_id_from_pos,
            'mempool.get_fee_histogram': self.compact_fee_histogram,
            'server.add_peer': self.add_peer,
            'server.banner': self.banner,
            'server.donation_address': self.donation_address,
            'server.features': self.server_features_async,
            'server.peers.subscribe': self.peers_subscribe,
            'server.ping': self.ping,
            'server.version': self.server_version,
        }

        # TESTING
        handlers['server.our_stats'] = self.get_session_stats
        # END TESTING

        if ptuple >= (1, 4, 2):
            handlers['blockchain.scripthash.unsubscribe'] = self.scripthash_unsubscribe

        if ptuple >= (1, 8):
            handlers['blockchain.scripthash.get_asset_balance'] = self.scripthash_get_asset_balance
            handlers['blockchain.scripthash.listassets'] = self.scripthash_listassets
            handlers['blockchain.asset.get_meta'] = self.asset_get_meta
            handlers['blockchain.asset.subscribe'] = self.asset_subscribe
            handlers['blockchain.asset.unsubscribe'] = self.asset_unsubscribe

        if ptuple >= (1, 9):
            handlers['blockchain.asset.is_qualified'] = self.is_qualified
            handlers['blockchain.asset.get_restricted_associations_current'] = self.get_restricted_associations_current
            handlers['blockchain.asset.get_restricted_associations_history'] = self.get_restricted_associations_history
            handlers['blockchain.asset.get_qualifier_associations_current'] = self.get_qualifier_associations_current
            handlers['blockchain.asset.get_qualifier_associations_history'] = self.get_qualifier_associations_history
            handlers['blockchain.asset.get_tags_for_h160_current'] = self.get_tags_for_h160_current
            handlers['blockchain.asset.get_tags_for_h160_history'] = self.get_tags_for_h160_history
            handlers['blockchain.asset.get_h160_for_asset_current'] = self.get_h160_for_asset_current
            handlers['blockchain.asset.get_h160_for_asset_history'] = self.get_h160_for_asset_history
            handlers['blockchain.asset.frozen_status_current'] = self.frozen_status_current
            handlers['blockchain.asset.frozen_status_history'] = self.frozen_status_history
            handlers['blockchain.asset.broadcasts'] = self.get_messages
            handlers['blockchain.asset.get_assets_with_prefix'] = self.get_assets_with_prefix

        self.request_handlers = handlers


class LocalRPC(SessionBase):
    '''A local TCP RPC server session.'''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = 'RPC'
        self.connection.max_response_size = 0
        self.request_handlers = self.session_mgr.rpc_request_handlers

    def protocol_version_string(self):
        return 'RPC'
