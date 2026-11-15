// 仅发送路径严格判定回执；未知 HTTP/正文保留未确认，不影响通用请求语义。
const PRE_ACCEPTANCE_UNAVAILABLE = new Set([
  'audit unavailable', 'state persist unavailable', 'state unavailable (restricted recovery)',
])

export async function requestSubmission(runId, text) {
  const response = await fetch(`/api/runs/${runId}/messages`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  const body = await response.text()
  let data
  try {
    data = JSON.parse(body)
  } catch (parseError) {
    return { status: 'unknown', body, parseError }
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return { status: 'unknown' }
  if (response.status === 200 && data.run_id === runId && data.status === 'RUNNING') {
    return { status: 'accepted' }
  }
  const detail = data.detail
  const validationErrors = response.status === 422 && Array.isArray(detail) && detail.length > 0
    && detail.every(item => item && Array.isArray(item.loc) && typeof item.msg === 'string')
  const apiError = (typeof detail === 'string' && detail.length > 0) || validationErrors
  const rejected = apiError && ([401, 403, 404, 409, 422].includes(response.status)
    || (response.status === 503 && PRE_ACCEPTANCE_UNAVAILABLE.has(detail)))
  return rejected
    ? { status: 'rejected', httpStatus: response.status,
        detail: validationErrors ? detail.map(item => item.msg).join('；') : detail }
    : { status: 'unknown' }
}
