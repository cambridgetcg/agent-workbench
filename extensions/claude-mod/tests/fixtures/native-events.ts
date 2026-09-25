/** Public journal events from native.py, bridge.py and runner.py. */
export const NATIVE_EVENTS = [
  { kind: 'native_session', session_id: 'session-pointer' },
  { kind: 'native_session_state', status: 'running' },
  { kind: 'native_tool', tool: 'Bash', status: 'completed' },
  { kind: 'native_turn', status: 'succeeded' },
  { kind: 'native_message', role: 'assistant' },
  { kind: 'native_task', status: 'started' },
  { kind: 'native_control', action: 'input', status: 'acknowledged' },
  { kind: 'native_control_queued', action: 'interrupt', status: 'queued' },
  { kind: 'native_input_closed', reason: 'settled_turns_heuristic' },
  { kind: 'native_decision_required', decision_type: 'permission' },
  { kind: 'native_protocol_error', code: 'malformed_json' },
]
