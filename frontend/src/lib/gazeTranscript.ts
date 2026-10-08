/** 同じ視線の反応は会話の応答と同じく続けて表示し、次の反応では置き換えます。 */
export function appendGazeTranscript(current: string, text: string, beatIndex: number): string {
  const line = text.trim()
  if (!line) return current
  if (beatIndex <= 0 || !current.trim()) return line
  return `${current}\n\n${line}`
}
