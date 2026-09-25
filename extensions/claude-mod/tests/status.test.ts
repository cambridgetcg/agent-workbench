import { describe, expect, test, tier } from 'claude-code/testing'
import { statusLines } from '../hooks/model'
import { NATIVE_EVENTS } from './fixtures/native-events'

tier('user')

const RUN = '1234567890abcdef1234567890abcdef'
const STATUS = { id: RUN, status: 'running', revision: 3,
  tasks: [{ id: 'private-task-title', harness: 'claude', status: 'running', attempts: 1,
    result: { summary: 'PRIVATE RESULT' } }], goal: 'PRIVATE GOAL', queued_guidance_count: 2 }
const TIMELINE = { id: RUN, entries: [{ kind: 'task_started', reason: 'PRIVATE REASON' }] }

describe('Workbench status projection', () => {
  test('excludes task content, arbitrary labels, and raw error messages', () => {
    const text = statusLines(JSON.stringify(STATUS), JSON.stringify(TIMELINE), RUN.slice(0, 8)).join('\n')
    expect(text).toContain('claude: running; attempts 1')
    expect(text).toContain('Queued guidance: 2')
    expect(text).not.toContain('PRIVATE')
    expect(text).not.toContain('private-task-title')
  })

  test('refuses mixed run identities', () => {
    expect(() => statusLines(JSON.stringify(STATUS), JSON.stringify({ id: 'f'.repeat(32) }), RUN.slice(0, 8))).toThrow()
  })

  test('unknown native values stay unknown', () => {
    const text = statusLines(JSON.stringify({ ...STATUS, status: 'SECRET', tasks: [{ harness: 'SECRET', status: 'SECRET' }] }),
      JSON.stringify(TIMELINE), RUN).join('\n')
    expect(text).toContain('unknown')
    expect(text).not.toContain('SECRET')
  })

  test('public native journal kinds retain their names without exposing payloads', () => {
    for (let index = 0; index < NATIVE_EVENTS.length; index += 5) {
      const entries = NATIVE_EVENTS.slice(index, index + 5)
      const text = statusLines(JSON.stringify(STATUS), JSON.stringify({ id: RUN, entries }), RUN).join('\n')
      expect(text).toContain(`Recent events: ${entries.map(event => event.kind).join(', ')}`)
      expect(text).not.toContain('unknown')
      expect(text).not.toContain('session-pointer')
      expect(text).not.toContain('settled_turns_heuristic')
    }
  })

  test('retry, recovery and guidance use emitted kinds; nonexistent kinds stay unknown', () => {
    const entries = ['task_retry_requested', 'run_recovered', 'guidance_queued', 'handoff_imported', 'workspace_removed']
      .map(kind => ({ kind }))
    const text = statusLines(JSON.stringify(STATUS), JSON.stringify({ id: RUN, entries }), RUN).join('\n')
    expect(text).toContain(`Recent events: ${entries.map(event => event.kind).join(', ')}`)
    const nonexistent = ['native_tool_started', 'native_tool_finished', 'native_turn_completed', 'task_retried']
      .map(kind => ({ kind }))
    const rejected = statusLines(JSON.stringify(STATUS), JSON.stringify({ id: RUN, entries: nonexistent }), RUN).join('\n')
    expect(rejected).toContain('Recent events: unknown, unknown, unknown, unknown')
  })
})

describe('Workbench native hooks', () => {
  test('start only registers a command; bad input never starts a process', async ($, on) => {
    const commands: string[] = []
    const processes: unknown[] = []
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('command.register', ($, e) => { commands.push(e.name); return { value: { command: e.name } } })
    on('process.run', ($, e) => { processes.push(e); return { value: { exitCode: 1, stdout: '', stderr: '' } } })
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    expect(commands).toEqual(['workbench-status'])
    const output = await $.command.run({ command: 'workbench-status', args: '12345678; launch anything',
      origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    expect(output.text).toContain('Usage:')
    expect(processes).toEqual([])
  })

  test('headless command reads only status and timeline and emits no model context', async ($, on) => {
    const argv: string[][] = []
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('command.register', ($, e) => ({ value: { command: e.name } }))
    on('ui.invalidate', () => ({ value: undefined }))
    on('process.run', ($, e) => {
      argv.push([...e.argv])
      expect(e.init?.timeoutMs).toBe(5000)
      return { value: { exitCode: 0, stdout: JSON.stringify(e.argv[1] === 'status' ? STATUS : TIMELINE), stderr: 'PRIVATE STDERR' } }
    })
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    const output = await $.command.run({ command: 'workbench-status', args: RUN.slice(0, 8),
      origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    expect(argv).toEqual([
      ['agent-workbench', 'status', RUN.slice(0, 8), '--metadata', '--json'],
      ['agent-workbench', 'timeline', RUN.slice(0, 8), '--limit', '5', '--json'],
    ])
    expect(output.text).toContain('Run 1234567890ab: running')
    expect(output.context).toBeUndefined()
    expect(output.text).not.toContain('PRIVATE')
  })

  test('failed host commands do not disclose stderr or launch a second command', async ($, on) => {
    let runs = 0
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('command.register', ($, e) => ({ value: { command: e.name } }))
    on('ui.invalidate', () => ({ value: undefined }))
    on('process.run', () => {
      runs += 1
      return { value: { exitCode: 1, stdout: '', stderr: 'PRIVATE SECRET' } }
    })
    await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
    const output = await $.command.run({ command: 'workbench-status', args: RUN,
      origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    expect(runs).toBe(1)
    expect(output.text).toContain('unavailable')
    expect(output.text).not.toContain('PRIVATE')
  })

  test('interactive command opens an owned pane and renders the selected snapshot', async ($, on) => {
    const opened: string[] = []
    const closed: string[] = []
    let calls = 0
    on('session.start', ($, e) => ({ cwd: e.cwd }))
    on('command.register', ($, e) => ({ value: { command: e.name } }))
    on('ui.invalidate', () => ({ value: undefined }))
    on('ui.open', ($, e) => { opened.push(e.id); return { value: undefined } })
    on('ui.close', ($, e) => { closed.push(e.id); return { value: undefined } })
    on('process.run', ($, e) => {
      calls += 1
      return { value: { exitCode: 0, stdout: JSON.stringify(e.argv[1] === 'status' ? STATUS : TIMELINE), stderr: '' } }
    })
    await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
    await $.command.run({ command: 'workbench-status', args: RUN,
      origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 80 } })
    expect(opened).toEqual(['agent-workbench-status'])
    const tree = await $.ui.render({ component: 'Pane', surface: 'terminal', requestId: 'agent-workbench-status',
      props: { title: 'Workbench', isFocused: false, bodyColumns: 80, placement: 'inline',
        scroll: { offset: 0, bodyRows: 20 }, view: {} } })
    const serialized = JSON.stringify(tree)
    expect(serialized).toContain('Run 1234567890ab: running')
    expect(serialized).not.toContain('PRIVATE')
    await $.ui.press({ plugin: 'agent-workbench-status', key: 'refresh' })
    expect(calls).toBe(4)
    await $.ui.press({ plugin: 'agent-workbench-status', key: 'close' })
    expect(closed).toEqual(['agent-workbench-status'])
    expect(calls).toBe(4)
  })
})
