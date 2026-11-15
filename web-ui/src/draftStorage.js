// 同一浏览器只保留当前用户的一份草稿记录，不保存凭据或提交历史。
const KEY = 'va-session-drafts'
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const hasDraftFields = value => isObject(value) && typeof value.text === 'string'
  && Number.isSafeInteger(value.version) && value.version >= 0
const isPending = submission => ['sending', 'unknown'].includes(submission?.status)

export function readDrafts(username) {
  const raw = localStorage.getItem(KEY)
  let saved
  try { saved = JSON.parse(raw) } catch { return {} }
  if (!isObject(saved) || saved.version !== 1 || saved.username !== username || !isObject(saved.drafts)) return {}
  return Object.fromEntries(Object.entries(saved.drafts).filter(([, draft]) =>
    hasDraftFields(draft) && (draft.submission == null || (hasDraftFields(draft.submission) && isPending(draft.submission)))
  ).map(([id, draft]) => [id, {
    text: draft.text, version: draft.version,
    ...(draft.submission ? { submission: { text: draft.submission.text, version: draft.submission.version, status: 'unknown' } } : {}),
  }]))
}

export function writeDrafts(username, records) {
  const drafts = Object.fromEntries(Object.entries(records)
    .filter(([, draft]) => draft.text || isPending(draft.submission))
    .map(([id, draft]) => [id, {
      text: draft.text, version: draft.version,
      ...(isPending(draft.submission) ? { submission: {
        text: draft.submission.text, version: draft.submission.version, status: draft.submission.status,
      } } : {}),
    }]))
  if (Object.keys(drafts).length) localStorage.setItem(KEY, JSON.stringify({ version: 1, username, drafts }))
  else removeDrafts()
}

export function removeDrafts() {
  localStorage.removeItem(KEY)
}
