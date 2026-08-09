import React from 'react';
import {
  CalendarClock,
  CirclePlus,
  Pencil,
  Trash2,
} from 'lucide-react';
import type { DeliveryMutationState } from '../../hooks/useDeliveryBundleMutation';
import type {
  CustomBlock,
  DayPlanV2,
  PublicDeliveryBundle,
  PublicDiningStop,
  PublicLodgingStay,
  PublicVisitStop,
  WorkspaceV2MutationOperation,
} from '../../types/delivery';

/**
 * 交通腿**不在**这里 —— 交通是只读的，编辑器不接受它。这是类型层面的同一句话：
 * `TimelineEntityDetails` 在装配编辑器之前就已经把交通行 return 掉了，两处不会漂开。
 */
type EditableEntity = PublicVisitStop | PublicDiningStop | PublicLodgingStay | CustomBlock;

interface MutationController {
  state: DeliveryMutationState;
  busy: boolean;
  previewItineraryOperation: (
    operation: WorkspaceV2MutationOperation,
    contextId: string
  ) => Promise<boolean>;
  confirm: () => Promise<boolean>;
  cancel: () => void;
  retry: () => Promise<boolean>;
}

interface EntityEditorProps {
  bundle: PublicDeliveryBundle;
  entity: EditableEntity;
  mutation: MutationController;
}

function entityId(entity: EditableEntity): string {
  if (entity.type === 'lodging_stay') return entity.stay_id;
  return entity.item_id;
}

function entityLabel(entity: EditableEntity): string {
  if (entity.type === 'visit_stop') return '景点安排';
  if (entity.type === 'dining_stop') return '用餐时间';
  if (entity.type === 'lodging_stay') return '整段住宿';
  return '自定义安排';
}

function inputTime(value: string | null): string {
  return value?.match(/T(\d{2}:\d{2})/)?.[1] ?? '';
}

function dateTimeFor(
  day: DayPlanV2 | undefined,
  previous: string | null,
  time: string
): string | null {
  if (!time) return null;
  const date = day?.date ?? previous?.slice(0, 10);
  if (!date) return previous;
  const zone = previous?.match(/(Z|[+-]\d{2}:\d{2})$/)?.[1] ?? '';
  return `${date}T${time}:00${zone}`;
}


function MutationNotice({ contextId, mutation }: { contextId: string; mutation: MutationController }) {
  const { state } = mutation;
  if (state.pending?.slotId !== contextId) return null;
  if (state.status === 'awaiting_confirmation') {
    return (
      <div role="alert" data-testid={`itinerary-confirmation-${contextId}`} className="mt-3 rounded-card border border-accent/25 bg-[var(--color-accent-soft)] px-3 py-3">
        {state.pending.preview?.impacts.map((impact) => (
          <p key={`${impact.kind}-${impact.summary}`} className="mt-1 break-words text-xs leading-5 text-ink-secondary">{impact.summary}</p>
        ))}
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" disabled={mutation.busy} onClick={() => void mutation.confirm()} className="rounded-card bg-accent px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-[var(--color-accent-hover)] disabled:cursor-wait disabled:opacity-60">确认调整</button>
          <button type="button" disabled={mutation.busy} onClick={mutation.cancel} className="rounded-card border border-stroke bg-panel px-3 py-1.5 text-xs font-semibold text-ink transition-colors hover:bg-surface disabled:opacity-60">返回</button>
        </div>
      </div>
    );
  }
  if (state.status === 'conflict' || state.status === 'failed') {
    return (
      <div role="alert" className="mt-3 rounded-card border border-error/25 bg-panel px-3 py-3">
        <p className="break-words text-xs leading-5 text-ink-secondary">{state.message}</p>
        <button type="button" disabled={mutation.busy} onClick={() => void mutation.retry()} className="mt-2 rounded-card border border-stroke bg-surface px-3 py-1.5 text-xs font-semibold text-ink transition-colors hover:text-accent disabled:cursor-wait disabled:opacity-60">基于最新行程重试</button>
      </div>
    );
  }
  if (['previewing', 'saving', 'confirming'].includes(state.status)) {
    return <p role="status" className="mt-3 text-xs text-ink-secondary">{state.message}</p>;
  }
  if (state.status === 'saved') {
    return <p role="status" className="mt-3 text-xs font-semibold text-accent">{state.message}</p>;
  }
  return null;
}

function DayAndScheduleEditor({
  entity,
  bundle,
  mutation,
}: {
  entity: PublicVisitStop | PublicDiningStop;
  bundle: PublicDeliveryBundle;
  mutation: MutationController;
}) {
  const days = bundle.workspace.itinerary.day_plans;
  const [targetDayId, setTargetDayId] = React.useState(entity.day_id);
  const [start, setStart] = React.useState(inputTime(entity.planned_start));
  const [end, setEnd] = React.useState(inputTime(entity.planned_end));
  const [duration, setDuration] = React.useState(String(entity.duration_minutes));
  React.useEffect(() => {
    setTargetDayId(entity.day_id);
    setStart(inputTime(entity.planned_start));
    setEnd(inputTime(entity.planned_end));
    setDuration(String(entity.duration_minutes));
  }, [entity.day_id, entity.duration_minutes, entity.item_id, entity.planned_end, entity.planned_start]);
  const currentDay = days.find((day) => day.day_id === entity.day_id);

  return (
    <div className="space-y-4">
      <div>
        <label className="text-xs font-semibold text-ink" htmlFor={`move-${entity.item_id}`}>安排日期</label>
        <div className="mt-1.5 flex min-w-0 flex-wrap gap-2">
          <select id={`move-${entity.item_id}`} value={targetDayId} onChange={(event) => setTargetDayId(event.target.value)} disabled={mutation.busy} className="min-w-0 flex-1 rounded-card border border-stroke bg-panel px-3 py-2 text-sm text-ink">
            {days.map((day) => <option key={day.day_id} value={day.day_id}>第 {day.day} 天{day.date ? ` · ${day.date}` : ''}</option>)}
          </select>
          <button type="button" disabled={mutation.busy || targetDayId === entity.day_id} onClick={() => void mutation.previewItineraryOperation({ type: 'move_timeline_item', item_id: entity.item_id, to_day_id: targetDayId }, entity.item_id)} className="rounded-card border border-stroke bg-panel px-3 text-xs font-semibold text-ink transition-colors hover:bg-surface disabled:cursor-default disabled:opacity-45">移动</button>
        </div>
        <p className="mt-1 text-[11px] leading-5 text-ink-muted">移动后，相邻的市内路线会作废并按新顺序重新计算。</p>
      </div>

      <fieldset>
        <legend className="text-xs font-semibold text-ink">当地时间</legend>
        <div className="mt-1.5 grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-[1fr_1fr_7rem_auto]">
          <label className="min-w-0 text-[11px] text-ink-muted">开始<input aria-label={`${entity.name}开始时间`} type="time" value={start} onChange={(event) => setStart(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
          <label className="min-w-0 text-[11px] text-ink-muted">结束<input aria-label={`${entity.name}结束时间`} type="time" value={end} onChange={(event) => setEnd(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
          <label className="min-w-0 text-[11px] text-ink-muted">分钟<input aria-label={`${entity.name}停留分钟`} type="number" min="1" inputMode="numeric" value={duration} onChange={(event) => setDuration(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
          <button type="button" disabled={mutation.busy || !duration || Number(duration) < 1 || (!!start && !!end && end <= start)} onClick={() => void mutation.previewItineraryOperation({
            type: 'update_stop_schedule',
            item_id: entity.item_id,
            planned_start: dateTimeFor(currentDay, entity.planned_start, start),
            planned_end: dateTimeFor(currentDay, entity.planned_end, end),
            duration_minutes: Number(duration),
          }, entity.item_id)} className="col-span-2 mt-5 rounded-card bg-ink px-3 text-xs font-semibold text-panel transition-colors hover:bg-ink-secondary disabled:cursor-default disabled:opacity-45 sm:col-span-1">保存时间</button>
        </div>
        {!!start && !!end && end <= start && <p role="alert" className="mt-1 text-xs leading-5 text-error">结束时间需要晚于开始时间。</p>}
      </fieldset>
    </div>
  );
}

function CustomEditor({ bundle, block, mutation }: { bundle: PublicDeliveryBundle; block: CustomBlock; mutation: MutationController }) {
  const days = bundle.workspace.itinerary.day_plans;
  const currentDay = days.find((day) => day.day_id === block.day_id);
  const [targetDayId, setTargetDayId] = React.useState(block.day_id);
  const [title, setTitle] = React.useState(block.title);
  const [note, setNote] = React.useState(block.note ?? '');
  const [start, setStart] = React.useState(inputTime(block.planned_start));
  const [end, setEnd] = React.useState(inputTime(block.planned_end));
  const [duration, setDuration] = React.useState(block.duration_minutes == null ? '' : String(block.duration_minutes));
  React.useEffect(() => {
    setTargetDayId(block.day_id);
    setTitle(block.title);
    setNote(block.note ?? '');
    setStart(inputTime(block.planned_start));
    setEnd(inputTime(block.planned_end));
    setDuration(block.duration_minutes == null ? '' : String(block.duration_minutes));
  }, [block]);
  const invalidTime = !!start && !!end && end <= start;

  return (
    <div className="space-y-4">
      <div className="grid min-w-0 gap-2 sm:grid-cols-2">
        <label className="text-[11px] text-ink-muted">标题<input value={title} maxLength={120} onChange={(event) => setTitle(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-3 text-sm text-ink" /></label>
        <label className="text-[11px] text-ink-muted">安排日期<select value={targetDayId} onChange={(event) => setTargetDayId(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-3 text-sm text-ink">{days.map((day) => <option key={day.day_id} value={day.day_id}>第 {day.day} 天{day.date ? ` · ${day.date}` : ''}</option>)}</select></label>
      </div>
      <label className="block text-[11px] text-ink-muted">备注<textarea value={note} maxLength={600} rows={3} onChange={(event) => setNote(event.target.value)} className="mt-1 block w-full resize-y rounded-card border border-stroke bg-panel px-3 py-2 text-sm leading-6 text-ink" /></label>
      <div className="grid min-w-0 grid-cols-3 gap-2">
        <label className="min-w-0 text-[11px] text-ink-muted">开始<input aria-label={`${block.title}开始时间`} type="time" value={start} onChange={(event) => setStart(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
        <label className="min-w-0 text-[11px] text-ink-muted">结束<input aria-label={`${block.title}结束时间`} type="time" value={end} onChange={(event) => setEnd(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
        <label className="min-w-0 text-[11px] text-ink-muted">分钟<input aria-label={`${block.title}持续分钟`} type="number" min="1" inputMode="numeric" value={duration} onChange={(event) => setDuration(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
      </div>
      {invalidTime && <p role="alert" className="text-xs leading-5 text-error">结束时间需要晚于开始时间。</p>}
      <div className="flex min-w-0 flex-wrap gap-2">
        <button type="button" disabled={mutation.busy || !title.trim() || invalidTime || (!!duration && Number(duration) < 1)} onClick={() => void mutation.previewItineraryOperation({
          type: 'update_custom_block',
          item_id: block.item_id,
          title: title.trim(),
          note: note.trim() || null,
          planned_start: dateTimeFor(currentDay, block.planned_start, start),
          planned_end: dateTimeFor(currentDay, block.planned_end, end),
          duration_minutes: duration ? Number(duration) : null,
        }, block.item_id)} className="rounded-card bg-ink px-3 text-xs font-semibold text-panel hover:bg-ink-secondary disabled:opacity-45">保存内容</button>
        <button type="button" disabled={mutation.busy || targetDayId === block.day_id} onClick={() => void mutation.previewItineraryOperation({ type: 'move_timeline_item', item_id: block.item_id, to_day_id: targetDayId }, block.item_id)} className="rounded-card border border-stroke bg-panel px-3 text-xs font-semibold text-ink hover:bg-surface disabled:opacity-45">移动日期</button>
        <button type="button" disabled={mutation.busy} onClick={() => void mutation.previewItineraryOperation({ type: 'delete_custom_block', item_id: block.item_id }, block.item_id)} className="inline-flex items-center gap-1.5 rounded-card px-3 text-xs font-semibold text-error hover:bg-error/5 disabled:opacity-45"><Trash2 size={13} /> 删除</button>
      </div>
    </div>
  );
}

export function ItineraryEntityEditor({ bundle, entity, mutation }: EntityEditorProps) {
  const id = entityId(entity);
  const [open, setOpen] = React.useState(false);
  const trigger = React.useRef<HTMLButtonElement>(null);
  const close = () => {
    setOpen(false);
    window.requestAnimationFrame(() => trigger.current?.focus());
  };

  return (
    <div data-testid={`entity-editor-${id}`} className="min-w-0 pb-2">
      {!open ? (
        <button ref={trigger} type="button" aria-expanded={false} onClick={() => setOpen(true)} className="inline-flex min-h-9 items-center gap-1.5 rounded-card px-2 text-[11px] font-semibold text-ink-muted transition-colors hover:bg-surface hover:text-ink"><Pencil size={12} /> 调整{entityLabel(entity)}</button>
      ) : (
        <section aria-label={`调整${entityLabel(entity)}`} className="mb-2 min-w-0 rounded-card border border-stroke bg-surface/45 px-3 py-3 sm:px-4">
          <div className="mb-3 flex min-w-0 items-center justify-between gap-3">
            <p className="inline-flex min-w-0 items-center gap-1.5 text-xs font-semibold text-ink"><CalendarClock size={14} className="shrink-0 text-accent" /> 调整{entityLabel(entity)}</p>
            <button type="button" onClick={close} className="shrink-0 rounded-card px-2 text-xs font-semibold text-ink-secondary hover:bg-panel">收起</button>
          </div>
          {(entity.type === 'visit_stop' || entity.type === 'dining_stop') && <DayAndScheduleEditor entity={entity} bundle={bundle} mutation={mutation} />}
          {entity.type === 'custom_block' && <CustomEditor bundle={bundle} block={entity} mutation={mutation} />}
          {entity.type === 'lodging_stay' && (
            <div>
              <p className="text-xs leading-5 text-ink-secondary">删除会同时移除 {entity.check_in_date} 至 {entity.check_out_date} 的整段住宿，以及每天的入住和退房节点。</p>
              <button type="button" disabled={mutation.busy} onClick={() => void mutation.previewItineraryOperation({ type: 'delete_lodging_stay', stay_id: entity.stay_id }, entity.stay_id)} className="mt-3 inline-flex items-center gap-1.5 rounded-card px-3 text-xs font-semibold text-error hover:bg-error/5 disabled:opacity-45"><Trash2 size={13} /> 删除整段住宿</button>
            </div>
          )}
          <MutationNotice contextId={id} mutation={mutation} />
        </section>
      )}
    </div>
  );
}

export function AddCustomBlockPanel({
  day,
  mutation,
}: {
  day: DayPlanV2;
  mutation: MutationController;
}) {
  const contextId = `add-custom-${day.day_id}`;
  const [open, setOpen] = React.useState(false);
  const [title, setTitle] = React.useState('');
  const [note, setNote] = React.useState('');
  const [start, setStart] = React.useState('');
  const [end, setEnd] = React.useState('');
  const [duration, setDuration] = React.useState('');
  const invalidTime = !!start && !!end && end <= start;
  React.useEffect(() => {
    if (mutation.state.status !== 'saved' || mutation.state.pending?.slotId !== contextId) return;
    setOpen(false);
    setTitle('');
    setNote('');
    setStart('');
    setEnd('');
    setDuration('');
  }, [contextId, mutation.state.pending?.slotId, mutation.state.status]);

  return (
    // 保存成功的确认要比表单活得久：表单在 saved 后自动收起，通知挂在这个常驻容器上，
    // 与 SelectionSlotCard 一致——承载通知的父级不随这次 mutation 一起卸载。
    <div className="min-w-0">
      {!open ? (
        <button type="button" data-testid={`add-custom-${day.day_id}`} onClick={() => setOpen(true)} className="mt-2 inline-flex items-center gap-1.5 rounded-card px-2 text-xs font-semibold text-ink-secondary transition-colors hover:bg-surface hover:text-accent"><CirclePlus size={14} /> 添加自定义安排</button>
      ) : (
        <section aria-label={`第 ${day.day} 天添加自定义安排`} className="mt-3 min-w-0 rounded-card border border-dashed border-stroke bg-surface/40 px-3 py-3 sm:px-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs font-semibold text-ink">添加自定义安排</p>
            <button type="button" onClick={() => setOpen(false)} className="rounded-card px-2 text-xs font-semibold text-ink-secondary hover:bg-panel">取消</button>
          </div>
          <div className="mt-3 grid min-w-0 gap-2 sm:grid-cols-2">
            <label className="text-[11px] text-ink-muted">标题<input autoFocus value={title} maxLength={120} onChange={(event) => setTitle(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-3 text-sm text-ink" /></label>
            <label className="text-[11px] text-ink-muted">备注<input value={note} maxLength={600} onChange={(event) => setNote(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-3 text-sm text-ink" /></label>
          </div>
          <div className="mt-2 grid min-w-0 grid-cols-3 gap-2">
            <label className="min-w-0 text-[11px] text-ink-muted">开始<input aria-label={`第 ${day.day} 天自定义开始时间`} type="time" value={start} onChange={(event) => setStart(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
            <label className="min-w-0 text-[11px] text-ink-muted">结束<input aria-label={`第 ${day.day} 天自定义结束时间`} type="time" value={end} onChange={(event) => setEnd(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
            <label className="min-w-0 text-[11px] text-ink-muted">分钟<input aria-label={`第 ${day.day} 天自定义持续分钟`} type="number" min="1" inputMode="numeric" value={duration} onChange={(event) => setDuration(event.target.value)} className="mt-1 block min-h-10 w-full min-w-0 rounded-card border border-stroke bg-panel px-2 text-sm text-ink" /></label>
          </div>
          {invalidTime && <p role="alert" className="mt-1 text-xs leading-5 text-error">结束时间需要晚于开始时间。</p>}
          <button type="button" disabled={mutation.busy || !title.trim() || invalidTime || (!!duration && Number(duration) < 1)} onClick={() => {
            const itemId = `custom_${crypto.randomUUID()}`;
            void mutation.previewItineraryOperation({
              type: 'create_custom_block',
              block: {
                type: 'custom_block',
                item_id: itemId,
                day_id: day.day_id,
                title: title.trim(),
                note: note.trim() || null,
                planned_start: dateTimeFor(day, null, start),
                planned_end: dateTimeFor(day, null, end),
                duration_minutes: duration ? Number(duration) : null,
              },
            }, contextId);
          }} className="mt-3 inline-flex items-center gap-1.5 rounded-card bg-ink px-3 text-xs font-semibold text-panel hover:bg-ink-secondary disabled:cursor-default disabled:opacity-45"><CirclePlus size={13} /> 保存到第 {day.day} 天</button>
        </section>
      )}
      <MutationNotice contextId={contextId} mutation={mutation} />
    </div>
  );
}
