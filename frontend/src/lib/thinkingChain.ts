import type { ThinkingStep } from '../types/chat';

/**
 * Whether a thinking-chain step's duration should render as a **live ticking**
 * readout (seconds counting up).
 *
 * A step gets the live seconds only while the parent context is the active
 * generator AND the step itself is still running (`toolStatus === 'running'`).
 *
 * It deliberately does **not** depend on the step's position inside a merged
 * tool group.  During Deep research many calls of the *same* tool are merged
 * into one group (`groupThinkingSteps`), so the currently running call is
 * frequently **not** the group's last row.  Gating on "is the last index"
 * made the running task's readout silently disappear in
 * exactly that case — the per-step seconds were the one honest signal that a
 * long tool/study call was still alive.
 */
export function shouldShowLiveDuration(
  step: ThinkingStep,
  isActiveContext: boolean,
): boolean {
  return Boolean(isActiveContext && step.toolStatus === 'running');
}
