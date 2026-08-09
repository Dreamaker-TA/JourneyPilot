import { api } from './api';
import { apiErrorDetailString } from './apiErrorDetail';
import { isPublicDeliveryBundle, type PublicDeliveryBundle } from '../types/delivery';

/**
 * Bundle identity a 409 conflict reports as current, or null when the response
 * is not a conflict that names one.
 *
 * A revision conflict answers with identity only (`current_bundle_id`), the same
 * shape as `report_out_of_date`: the server must stay able to answer a conflict
 * without projecting the persisted Bundle, so the client re-reads it.
 */
export function conflictCurrentBundleId(error: unknown): string | null {
  return apiErrorDetailString(error, 'current_bundle_id');
}

/**
 * Re-read the server's current Bundle after a conflict.
 *
 * The id in the conflict detail is a hint that the head moved, not the answer:
 * the authoritative resync is the current-Bundle route. Throws when the read
 * does not belong to this run rather than leaving stale content on screen.
 */
export async function readCurrentBundleAfterConflict(
  runId: string,
  userId: string,
  sessionId: string | null
): Promise<PublicDeliveryBundle> {
  const bundle = await api.getCurrentDeliveryBundle(runId, userId, sessionId);
  if (!isPublicDeliveryBundle(bundle) || bundle.manifest.run_id !== runId) {
    throw new Error('current Delivery Bundle read after a conflict does not belong to this run');
  }
  return bundle;
}
