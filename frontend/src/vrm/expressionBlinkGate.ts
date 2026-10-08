/** 目標との差がこれを超える表情は、まだ切り替わっているものとして扱います。 */
export const EXPRESSION_TRANSITION_EPSILON = 0.02

/**
 * 表情の切替中だけまばたきを止める設定が有効で、
 * いずれかの表情ウェイトが目標から離れているかを返します。
 */
export function shouldHoldBlinkDuringExpression(
  enabled: boolean,
  weightGaps: readonly number[],
  epsilon = EXPRESSION_TRANSITION_EPSILON,
): boolean {
  if (!enabled) return false
  return weightGaps.some((gap) => Number.isFinite(gap) && gap > epsilon)
}
