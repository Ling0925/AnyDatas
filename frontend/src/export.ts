import type { QueryResponse } from './types'

export function downloadQueryCsv(result: QueryResponse, requestedName: string) {
  const lines = [
    result.columns.map((column) => csvCell(column.name)).join(','),
    ...result.rows.map((row) => row.map(csvCell).join(',')),
  ]
  const blob = new Blob([`\uFEFF${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' })
  downloadBlob(blob, `${safeFilename(requestedName)}.csv`)
}

function csvCell(value: unknown): string {
  let text = value === null || value === undefined
    ? ''
    : typeof value === 'object'
      ? JSON.stringify(value)
      : String(value)
  if (typeof value === 'string' && /^[=+\-@]/.test(text.trimStart())) text = `'${text}`
  return `"${text.replaceAll('"', '""')}"`
}

function safeFilename(value: string): string {
  const clean = value.trim().replace(/[\u0000-\u001f<>:"/\\|?*]+/g, '-').replace(/[. ]+$/g, '')
  return clean || 'anydatas-result'
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
