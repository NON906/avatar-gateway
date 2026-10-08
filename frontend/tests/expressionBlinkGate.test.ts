import assert from 'node:assert/strict'

import {
  EXPRESSION_TRANSITION_EPSILON,
  shouldHoldBlinkDuringExpression,
} from '../src/vrm/expressionBlinkGate.ts'

assert.equal(shouldHoldBlinkDuringExpression(false, [1]), false)
assert.equal(shouldHoldBlinkDuringExpression(true, [0, EXPRESSION_TRANSITION_EPSILON]), false)
assert.equal(
  shouldHoldBlinkDuringExpression(true, [0, EXPRESSION_TRANSITION_EPSILON + 0.001]),
  true,
)
assert.equal(shouldHoldBlinkDuringExpression(true, [Number.NaN, 0]), false)

console.log('expression blink gate ok')
