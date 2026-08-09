import React from 'react';
import { ArrowRight } from 'lucide-react';
import { useApp } from '../../context/AppContext';
import type { PlaceIdentity, RouteDecision } from '../../types/api';
import { Button } from '../ui/Button';
import { PlaceField } from './TripPlanner';

export const DiscoveryToIntake: React.FC<{ rawInput: string }> = ({ rawInput }) => {
  const { dispatch } = useApp();
  const [place, setPlace] = React.useState<PlaceIdentity | null>(null);

  const start = () => {
    if (!place) return;
    const decision: RouteDecision = {
      route: 'trip_planning', confidence: 1, alternatives: [],
      signals: ['explicit_ui_action', 'destination_selected_after_discovery'],
      requires_trip_draft: true, requires_confirmation: false,
    };
    dispatch({
      type: 'SET_GUIDED_INTAKE',
      payload: {
        raw_input: rawInput,
        route_decision: decision,
        controlled_identity: null,
        seed_destinations: [place],
        missing_fields: ['dates', 'party', 'style'],
        ready_to_create: false,
      },
    });
    dispatch({ type: 'SET_LAST_ROUTE_DECISION', payload: decision });
  };

  return (
    <section className="rounded-card border border-stroke bg-panel p-4 shadow-sm" aria-labelledby="discovery-to-trip-title">
      <h2 id="discovery-to-trip-title" className="text-base font-semibold text-ink">选好候选后，再创建旅行</h2>
      <p className="mt-1 text-sm text-ink-secondary">推荐与比较不会创建 TripRun。请用真实地点候选确认你决定去的城市或区域。</p>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-end">
        <PlaceField role="destination" value={place} onChange={setPlace} label="把候选变成旅行" />
        <Button type="button" disabled={!place} onClick={start}>用这个目的地创建旅行 <ArrowRight size={14} /></Button>
      </div>
    </section>
  );
};
