/** Wrap the caption itself; a display field name never changes its semantics. */
export function wrapGraphLabel(text: string, width = 158, lines = 2,
  measure: (value: string) => number = value => Array.from(value).reduce((sum, char) => sum + (/[^\u0000-\u00ff]/u.test(char) ? 13 : 7), 0)): string[] {
  const chars = Array.from(text.replace(/\s+/gu, ' ').trim())
  if (!chars.length) return ['']
  const result: string[] = []
  let index = 0
  while (index < chars.length && result.length < lines) {
    let line = ''
    while (index < chars.length && (!line || measure(line + chars[index]) <= width)) line += chars[index++]
    if (result.length === lines - 1 && index < chars.length) {
      const pieces = Array.from(line)
      while (pieces.length && measure(pieces.join('') + '…') > width) pieces.pop()
      line = pieces.join('') + '…'
    }
    result.push(line)
  }
  return result
}
