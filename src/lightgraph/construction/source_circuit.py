"""Run-local failure bounds, independent of the Agent's wording or draft IDs."""
from copy import deepcopy


class SourceCircuits:
    """Stop repeatedly failing sources without marking their mappings false.

    A successful data operation clears consecutive transient failures. Reading
    metadata, changing SQL/draft IDs or inspecting saved work cannot reset them.
    A new worker starts fresh; restoring credentials is an operator action.
    """

    failure_limit = 3
    blocked_request_limit = 3

    def __init__(self):
        self.states = {}

    def fail(self, source_id, diagnostic, *, terminal=False, observed=True):
        code = diagnostic.get('code')
        database = diagnostic.get('database') or {}
        terminal = terminal or code in {'database_reconnect_failed', 'source_catalog_changed', 'source_closed'}
        connection_fault = code in {'read_only_transaction_failed', 'snapshot_close_failed',
                                    'database_connection_failed'} or database.get('category') in {
                                        'connection', 'idle_transaction_timeout', 'shutdown', 'unavailable'}
        # A permission/setup rejection must not be retried as a network fault.
        terminal = terminal or (database.get('stage') in {'snapshot_start', 'reconnect'}
            and database.get('category') in {'authentication', 'permission', 'configuration_or_catalog', 'unsupported'})
        if not terminal and not connection_fault:
            return
        state = self.states.setdefault(source_id, {'source_id': source_id,
            'consecutive_failures': 0, 'blocked': False, 'blocked_requests': 0})
        if state['blocked']:
            return
        # Synchronizing the adapter's terminal state is not another failed
        # tool operation. Count an observed failure only once.
        state['consecutive_failures'] = (state['consecutive_failures'] + 1 if observed
                                         else max(1, state['consecutive_failures']))
        state['cause_code'] = code
        if database:
            state['database'] = deepcopy(database)
        state['blocked'] = terminal or state['consecutive_failures'] >= self.failure_limit

    def succeeded(self, source_id):
        state = self.states.get(source_id)
        if state and not state['blocked']:
            self.states.pop(source_id)

    def blocked(self, source_ids):
        return [deepcopy(self.states[s]) for s in sorted(set(source_ids))
                if self.states.get(s, {}).get('blocked')]

    def reject(self, source_ids):
        blocked = self.blocked(source_ids)
        for state in blocked:
            self.states[state['source_id']]['blocked_requests'] += 1
        return self.blocked(source_ids)

    def summary(self):
        return {'failure_limit': self.failure_limit,
                'blocked_request_limit': self.blocked_request_limit,
                'sources': [deepcopy(self.states[s]) for s in sorted(self.states)]}
