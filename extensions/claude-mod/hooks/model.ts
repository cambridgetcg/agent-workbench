/** Convert Workbench CLI JSON to an intentionally small, content-free view. */
const STATES = new Set(['ready', 'pending', 'running', 'paused', 'stopped', 'succeeded', 'failed', 'cancelled', 'interrupted'])
const HARNESSES = new Set(['codex', 'claude', 'astra', 'astra-ultra', 'command'])
const EVENTS = new Set(['created', 'run_started', 'run_succeeded', 'run_failed',
  'run_paused', 'run_stopped', 'run_interrupted', 'run_recovered',
  'task_started', 'task_finished', 'task_retry_requested',
  'pause_requested', 'stop_requested', 'guidance_queued', 'note_added',
  'handoff_imported', 'workspace_removed',
  'native_session', 'native_session_state', 'native_tool', 'native_turn', 'native_message',
  'native_task', 'native_control', 'native_control_queued', 'native_input_closed',
  'native_decision_required', 'native_protocol_error'])

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {}
}

function choice(value: unknown, choices: Set<string>): string {
  return typeof value === 'string' && choices.has(value) ? value : 'unknown'
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 1000000 ? value : null
}

export function statusLines(statusText: string, timelineText: string, requestedId: string): string[] {
  if (statusText.length > 1000000 || timelineText.length > 1000000) throw new Error('Oversized status response')
  const status = object(JSON.parse(statusText))
  if (typeof status.id !== 'string' || !/^[a-f0-9]{32}$/.test(status.id) || !status.id.startsWith(requestedId)) {
    throw new Error('Status response does not match the requested run')
  }
  const timeline = object(JSON.parse(timelineText))
  if (timeline.id !== status.id) throw new Error('Timeline response does not match the requested run')
  if (!Array.isArray(status.tasks) || status.tasks.length > 200) throw new Error('Invalid task list')
  const lines = [`Run ${status.id.slice(0, 12)}: ${choice(status.status, STATES)}`,
    `Tasks: ${status.tasks.length}; snapshot revision: ${count(status.revision) ?? 'unknown'}`]
  for (let index = 0; index < Math.min(status.tasks.length, 16); index += 1) {
    const task = object(status.tasks[index])
    lines.push(`${index + 1}. ${choice(task.harness, HARNESSES)}: ${choice(task.status, STATES)}; attempts ${count(task.attempts) ?? 'unknown'}`)
  }
  if (status.tasks.length > 16) lines.push(`${status.tasks.length - 16} more tasks; use the Workbench CLI for details.`)
  lines.push(`Queued guidance: ${count(status.queued_guidance_count) ?? 'unknown'}`)
  if (Array.isArray(timeline.entries)) {
    const events = timeline.entries.slice(-5).map(entry => choice(object(entry).kind, EVENTS))
    if (events.length) lines.push(`Recent events: ${events.join(', ')}`)
  }
  lines.push('Recorded snapshot; process liveness and correctness are not established.')
  return lines
}
