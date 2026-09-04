import { describe, expect, it } from 'vitest'
import { mergeRunEvents } from './eventMerge.js'

const event = (seq, type, payload = {}) => ({
  seq,
  type,
  payload: { ts: seq * 10, ...payload },
})

const initialRun = () => ({
  status: 'READY',
  stage: null,
  title: null,
  result: null,
  endedAt: null,
  lastEventAt: null,
  events: [],
  maxSeq: 0,
})

const observable = (run) => ({
  seqs: run.events.map((item) => item.seq),
  status: run.status,
  stage: run.stage,
  title: run.title,
  result: run.result,
  endedAt: run.endedAt,
  lastEventAt: run.lastEventAt,
  maxSeq: run.maxSeq,
})

const completeSnapshot = () => [
  event(1, 'session.started'),
  event(2, 'stage.changed', { stage: 'VERIFY' }),
  event(3, 'session.title_changed', { title: '验证 nginx' }),
  event(4, 'turn.completed', { result: '上一回合完成' }),
  event(5, 'turn.started'),
]

describe('mergeRunEvents', () => {
  it('tail 先于快照到达时仍按 seq 去重、排序并重算会话状态', () => {
    const snapshot = completeSnapshot()

    let run = mergeRunEvents(initialRun(), [snapshot[4]])
    run = mergeRunEvents(run, snapshot)

    expect(observable(run)).toEqual({
      seqs: [1, 2, 3, 4, 5],
      status: 'RUNNING',
      stage: 'VERIFY',
      title: '验证 nginx',
      result: '上一回合完成',
      endedAt: null,
      lastEventAt: 50_000,
      maxSeq: 5,
    })
  })

  it('快照先于 tail 且 tail 重复时得到完全相同的事件事实与派生字段', () => {
    const snapshot = completeSnapshot()

    let tailFirst = mergeRunEvents(initialRun(), [snapshot[4]])
    tailFirst = mergeRunEvents(tailFirst, snapshot)
    let snapshotFirst = mergeRunEvents(initialRun(), snapshot)
    snapshotFirst = mergeRunEvents(snapshotFirst, [snapshot[4]])

    expect(observable(snapshotFirst)).toEqual(observable(tailFirst))
  })

  it('断线 tail 有空洞且乱序时以最大 seq 为游标，快照补齐后无重复无空洞', () => {
    const snapshot = completeSnapshot()
    const later = event(6, 'agent.message', { text: '继续处理中' })

    let run = mergeRunEvents(initialRun(), [later, snapshot[2], snapshot[4]])
    expect(run.maxSeq).toBe(6)

    run = mergeRunEvents(run, [...snapshot, later])
    expect({ seqs: run.events.map((item) => item.seq), maxSeq: run.maxSeq }).toEqual({
      seqs: [1, 2, 3, 4, 5, 6],
      maxSeq: 6,
    })
  })

  it('摘要给出的 ENDED 在历史事件不含终态收尾时保持不可操作', () => {
    const ended = initialRun()
    ended.status = 'ENDED'
    ended.endedAt = 75_000

    const run = mergeRunEvents(ended, [event(5, 'turn.started')])

    expect({ status: run.status, endedAt: run.endedAt }).toEqual({
      status: 'ENDED',
      endedAt: 75_000,
    })
  })
})
