import type { EngineInterface, Register } from 'claude-code'
import { statusLines } from './model'

const PANE = 'agent-workbench-status'
const COMMAND = 'workbench-status'
const USAGE = 'Usage: /workbench-status RUN_ID (8–32 hex characters), or /workbench-status close'

type State = {
  executable: string
  cwd?: string
  interactiveTerminal: boolean
  selectedRun?: string
  generation: number
  lines: string[]
}

async function refresh($: EngineInterface, state: State, runId: string): Promise<string> {
  const ticket = ++state.generation
  let nextLines: string[]
  try {
    const status = await $.process.run([state.executable, 'status', runId, '--metadata', '--json'], { cwd: state.cwd, timeoutMs: 5000 })
    if (status.exitCode !== 0) throw new Error('Status unavailable')
    const timeline = await $.process.run([state.executable, 'timeline', runId, '--limit', '5', '--json'], { cwd: state.cwd, timeoutMs: 5000 })
    if (timeline.exitCode !== 0) throw new Error('Timeline unavailable')
    nextLines = statusLines(status.stdout, timeline.stdout, runId)
  } catch {
    // Native output and exception messages can contain private task content.
    nextLines = ['Workbench status unavailable. Check the run ID and Workbench CLI installation.']
  }
  if (ticket === state.generation) {
    state.selectedRun = runId
    state.lines = nextLines
    $.ui.invalidate('ui.render')
  }
  return nextLines.join('\n')
}

/** Original integration against the public Claude function-hooks contract. */
export const register: Register = (on, options) => {
  const executable = typeof options.executable === 'string' && options.executable.length > 0
    && options.executable.length <= 4096 && !/[\x00-\x1f]/.test(options.executable)
    ? options.executable : 'agent-workbench'
  const state: State = { executable, interactiveTerminal: false, generation: 0,
    lines: ['Run /workbench-status RUN_ID to inspect a saved run.'] }

  on('session.start', async ($, e, next) => {
    state.cwd = e.cwd
    state.interactiveTerminal = e.isInteractive && e.surface === 'terminal'
    await $.command.register({ name: COMMAND, description: 'Show a saved Workbench run without starting work.',
      argumentHint: 'RUN_ID | close', immediate: true })
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    const runId = e.args.trim()
    if (runId === 'close') {
      state.generation += 1
      await $.ui.close({ id: PANE })
      return { text: 'Workbench pane closed.' }
    }
    if (!/^[a-f0-9]{8,32}$/.test(runId)) return { text: USAGE }
    const ticket = state.generation + 1
    const text = await refresh($, state, runId)
    if (ticket === state.generation && state.interactiveTerminal
        && (!e.presentation?.isFullscreen || (e.presentation?.columns ?? 0) >= 110)) {
      try {
        await $.ui.open({ id: PANE, title: 'Workbench', closeOnEscape: true })
      } catch {
        // A denied or unsupported pane still leaves the read-only text result.
      }
    }
    return { text }
  })

  on('ui.render', { component: 'Pane' }, ($, e, next) => {
    if (e.requestId !== PANE || e.surface !== 'terminal') return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    return <Box flexDirection="column">
      <Text>{state.lines.join('\n')}</Text>
      <Box>
        <Button key="refresh" label="Refresh" onPress={async () => {
          if (state.selectedRun) await refresh($, state, state.selectedRun)
        }} />
        <Button key="close" label="Close" onPress={() => {
          state.generation += 1
          return $.ui.close({ id: PANE })
        }} />
      </Box>
    </Box>
  })
}
