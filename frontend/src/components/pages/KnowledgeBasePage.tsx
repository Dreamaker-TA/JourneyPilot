import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Plus, Trash2, Upload } from 'lucide-react';
import { Button } from '../ui/Button';
import { ConfirmAction } from '../ui/ConfirmAction';
import { TextArea } from '../ui/Input';
import { Modal } from '../ui/Modal';
import { PageShell, type SurfaceState } from '../ui/PageShell';
import { RuledList, RuledReadout, RuledRow } from '../ui/RuledList';
import { api } from '../../lib/api';
import { cn } from '../../lib/utils';
import { describeRequestFailure, type RequestFailure } from '../../lib/requestFailureMessage';
import {
  describeKnowledgeIngestFailure,
  describeKnowledgeSourceFailure,
} from '../../lib/knowledgeIngestFailure';
import type {
  KnowledgeCollectionStats,
  KnowledgeSourceDetail,
  KnowledgeSourceDocument,
} from '../../types/api';

/** 旅行者的默认资料库；名称对用户不可见，固定使用。 */
const DEFAULT_COLLECTION = 'travel_knowledge';

export const KnowledgeBasePage: React.FC = () => {
  const [stats, setStats] = useState<KnowledgeCollectionStats | null>(null);
  const [loadingStats, setLoadingStats] = useState(true);
  const [loadError, setLoadError] = useState<RequestFailure | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [isIndexing, setIsIndexing] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [manualContent, setManualContent] = useState('');
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusTone, setStatusTone] = useState<'ok' | 'error'>('ok');
  /** 当前打开的那一篇（来源名）。`null` = 没开弹层。 */
  const [openSource, setOpenSource] = useState<string | null>(null);
  /** 名字里带 `source` 是为了不遮住全局 `document`。 */
  const [sourceDocument, setSourceDocument] = useState<KnowledgeSourceDocument | null>(null);
  const [draft, setDraft] = useState('');
  const [loadingDocument, setLoadingDocument] = useState(false);
  const [documentError, setDocumentError] = useState<RequestFailure | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeletingSource, setIsDeletingSource] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const statsRequestIdRef = useRef(0);
  const documentRequestIdRef = useRef(0);

  const loadStats = useCallback(async () => {
    const requestId = ++statsRequestIdRef.current;
    setLoadingStats(true);
    setLoadError(null);
    try {
      const nextStats = await api.getKnowledgeCollectionStats(DEFAULT_COLLECTION);
      if (requestId !== statsRequestIdRef.current) return;
      setStats(nextStats);
    } catch (error) {
      if (requestId !== statsRequestIdRef.current) return;
      setStats(null);
      setLoadError(describeRequestFailure(error, '读取', '资料库'));
    } finally {
      if (requestId === statsRequestIdRef.current) setLoadingStats(false);
    }
  }, []);

  useEffect(() => {
    void loadStats();
    return () => {
      statsRequestIdRef.current += 1;
    };
  }, [loadStats]);

  /**
   * 打开一篇资料就去读它的正文。
   *
   * 请求序号与列表那一份同一副写法（后到的请求作废先到的），因为连点两行是这一屏
   * 最容易做出的动作，而两次读的返回顺序不由点击顺序决定。
   */
  useEffect(() => {
    if (openSource === null) return;
    const requestId = ++documentRequestIdRef.current;
    setLoadingDocument(true);
    setDocumentError(null);
    setSourceDocument(null);
    void (async () => {
      try {
        const next = await api.getKnowledgeSource(DEFAULT_COLLECTION, openSource);
        if (requestId !== documentRequestIdRef.current) return;
        setSourceDocument(next);
        setDraft(next.content);
      } catch (error) {
        if (requestId !== documentRequestIdRef.current) return;
        setDocumentError(describeKnowledgeSourceFailure(error, '读取'));
      } finally {
        if (requestId === documentRequestIdRef.current) setLoadingDocument(false);
      }
    })();
  }, [openSource]);

  const setStatus = (message: string, tone: 'ok' | 'error') => {
    setStatusTone(tone);
    setStatusMessage(message);
  };

  /** 关掉弹层就把这一篇的状态清干净：下一次打开不许先闪一下上一篇的正文。 */
  const closeSource = () => {
    documentRequestIdRef.current += 1;
    setOpenSource(null);
    setSourceDocument(null);
    setDocumentError(null);
    setDraft('');
    setLoadingDocument(false);
  };

  const handleSaveSource = async () => {
    if (!openSource || isSaving) return;
    setIsSaving(true);
    try {
      const result = await api.updateKnowledgeSource(DEFAULT_COLLECTION, openSource, draft);
      setStatus(
        `已保存「${openSource}」，重新整理成 ${result.chunks_indexed} 段资料。`,
        'ok'
      );
      closeSource();
      await loadStats();
    } catch (error) {
      setDocumentError(describeKnowledgeSourceFailure(error, '保存'));
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteSource = async () => {
    if (!openSource || isDeletingSource) return;
    setIsDeletingSource(true);
    try {
      const result = await api.deleteKnowledgeSource(DEFAULT_COLLECTION, openSource);
      setStatus(
        `已删除「${openSource}」，原有 ${result.deleted_chunks} 段资料。`,
        'ok'
      );
      closeSource();
      await loadStats();
    } catch (error) {
      setDocumentError(describeKnowledgeSourceFailure(error, '删除'));
    } finally {
      setIsDeletingSource(false);
    }
  };

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || isUploading) return;
    setIsUploading(true);
    try {
      const result = await api.knowledgeUploadFile(file, DEFAULT_COLLECTION);
      setStatus(`已把「${file.name}」加入资料库，整理成 ${result.chunks_indexed} 段资料。`, 'ok');
      await loadStats();
    } catch (error) {
      // 「这份文件没有正文」不是「请求信息不完整」——两者的下一步动作完全不同，见
      // `knowledgeIngestFailure.ts`。
      setStatus(describeKnowledgeIngestFailure(error, '上传').message, 'error');
    } finally {
      setIsUploading(false);
      e.target.value = '';
    }
  };

  const handleManualIndex = async () => {
    if (!manualContent.trim() || isIndexing) return;
    setIsIndexing(true);
    try {
      const result = await api.knowledgeIndex(manualContent, DEFAULT_COLLECTION);
      setManualContent('');
      setShowAddModal(false);
      setStatus(`已添加到资料库，整理成 ${result.chunks_indexed} 段资料。`, 'ok');
      await loadStats();
    } catch (error) {
      setStatus(describeKnowledgeIngestFailure(error, '添加').message, 'error');
    } finally {
      setIsIndexing(false);
    }
  };

  const handleDeleteCollection = async () => {
    if (isDeleting) return;
    setIsDeleting(true);
    try {
      await api.knowledgeDeleteCollection(DEFAULT_COLLECTION);
      setStats(null);
      setShowDeleteModal(false);
      setStatus('已清空资料库。', 'ok');
      await loadStats();
    } catch (error) {
      setStatus(describeRequestFailure(error, '删除', '资料库').message, 'error');
    } finally {
      setIsDeleting(false);
    }
  };

  const chunkCount = stats?.total ?? 0;
  const sourceDetails: KnowledgeSourceDetail[] = stats?.source_details ?? [];
  const sourceCount = stats?.sources ?? 0;
  const busy = isIndexing || isUploading || isDeleting;

  const surfaceState: SurfaceState = loadingStats
      ? { kind: 'loading' }
      : loadError
        ? {
            kind: 'error',
            title: '暂时读不到资料库状态',
            failure: loadError,
            onRetry: () => void loadStats(),
          }
        : sourceDetails.length === 0
          ? {
              kind: 'empty',
              mark: 'empty-sleeve',
              line: '还没有资料',
              hint: '上传攻略、签证清单或酒店备选，规划时会参考它们。',
            }
          : { kind: 'ready' };

  return (
    <>
      <PageShell
        title="资料来源"
        purpose="规划时会参考这里的资料"
        /* 读数走标题右侧那一档，不是正文顶上一句游离的 12px 小字。
           `N 段资料 · M 个来源` 是纯数字 + 中文量词的读数，不是句子。 */
        readout={
          !loadingStats && !loadError && chunkCount > 0
            ? `${chunkCount} 段 · ${sourceCount} 源`
            : undefined
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setShowAddModal(true)}
              disabled={busy}
            >
              <Plus size={14} />
              添加文本
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.md,.pdf,.docx"
              className="sr-only"
              onChange={handleFileUpload}
              disabled={isUploading || isDeleting}
            />
            <Button
              variant="primary"
              size="sm"
              loading={isUploading}
              disabled={isIndexing || isDeleting}
              type="button"
              onClick={() => fileInputRef.current?.click()}
            >
              <Upload size={14} />
              上传文件
            </Button>
          </>
        }
        state={surfaceState}
      >
        <RuledList count={sourceDetails.length}>
          {sourceDetails.map((detail) => (
            <RuledRow
              key={detail.source}
              title={detail.source}
              /* 一条能点的行**就是**打开这一篇的入口（`RuledList` 的合同）：不给 `onClick`
                 时它渲染成一段 `<div>`，那正是这一屏此前点不开任何东西的原因 —— 屏幕上
                 一行看起来像条目，点下去没有失败、也没有反应。 */
              onClick={() => setOpenSource(detail.source)}
              active={openSource === detail.source}
              testId={`knowledge-source-row-${detail.source}`}
              /* 行左边**不挂图标方块**（`bg-accent/10 text-accent` 那类）：那是一个不可点的
                 装饰占着 §Color 的交互声部（「main action, selected state, focus ring,
                 live confidence」）。来源名本身就是这一行的主词。 */
              trailing={<RuledReadout>{detail.chunk_count} 段</RuledReadout>}
            />
          ))}
        </RuledList>

        {/* 整库销毁是 T3：走 Modal，标题陈述后果。 */}
        <div className="mt-6 flex justify-end border-t border-stroke/60 pt-4">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowDeleteModal(true)}
            disabled={busy || (chunkCount === 0 && sourceCount === 0)}
          >
            <Trash2 size={14} />
            删除资料库
          </Button>
        </div>
      </PageShell>

      {/* 操作回执：不进正文流（那会把列表往下顶一格），停在屏底一条独立的通报上。 */}
      {statusMessage && (
        <StatusLine tone={statusTone} onDismiss={() => setStatusMessage(null)}>
          {statusMessage}
        </StatusLine>
      )}

      <Modal
        open={showAddModal}
        onClose={() => setShowAddModal(false)}
        title="添加旅行资料"
        maxWidth="max-w-2xl"
      >
        <TextArea
          placeholder="粘贴你的攻略、签证材料清单、酒店备选或交通说明..."
          value={manualContent}
          onChange={(e) => setManualContent(e.target.value)}
          rows={8}
          className="mb-4"
        />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" size="sm" onClick={() => setShowAddModal(false)}>
            取消
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={handleManualIndex}
            loading={isIndexing}
            disabled={!manualContent.trim() || isUploading || isDeleting}
          >
            添加到资料库
          </Button>
        </div>
      </Modal>

      {/* 一篇资料的正文：看、改、删都在这一枚弹层里（§3.3「阻断式决策、T3 确认、表单」）。 */}
      <Modal
        open={openSource !== null}
        onClose={closeSource}
        title={openSource ?? ''}
        maxWidth="max-w-3xl"
      >
        {loadingDocument ? (
          <p className="py-6 text-center text-sm text-ink-secondary">正在读取正文…</p>
        ) : documentError && !sourceDocument ? (
          /* 读不到正文时**不给编辑区**：一个空的输入框加一枚保存钮，等于邀请用户
             把这一篇的正文覆盖成空白。能做的动作只剩删除（用户这一轮的裁决：旧的删掉
             重新上传）。 */
          <div className="space-y-4">
            <p className="text-sm leading-relaxed text-error">{documentError.message}</p>
            <div className="flex justify-between gap-2">
              <ConfirmAction
                onConfirm={() => void handleDeleteSource()}
                confirmPending={isDeletingSource}
                confirmLabel="删除"
                testId="knowledge-source-delete"
              >
                <Trash2 size={14} />
                删除这篇
              </ConfirmAction>
              <Button variant="secondary" size="sm" onClick={closeSource}>
                关闭
              </Button>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            <TextArea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              rows={16}
              aria-label="资料正文"
            />
            {/* 段数是读数不是句子：保存会按新正文重新分段，所以它会变。 */}
            <p className="text-xs leading-relaxed text-ink-secondary">
              当前 {sourceDocument?.chunk_count ?? 0} 段 · 保存会按新正文重新分段，规划时参考的就是保存后的这一份
            </p>
            {documentError && (
              <p className="text-sm leading-relaxed text-error">{documentError.message}</p>
            )}
            <div className="flex items-center justify-between gap-2">
              {/* 删一篇是单体不可逆操作 = T2，走就地二段确认，不再套第二枚弹层。 */}
              <ConfirmAction
                onConfirm={() => void handleDeleteSource()}
                confirmPending={isDeletingSource}
                confirmLabel="删除"
                disabled={isSaving}
                testId="knowledge-source-delete"
              >
                <Trash2 size={14} />
                删除这篇
              </ConfirmAction>
              <div className="flex gap-2">
                <Button variant="secondary" size="sm" onClick={closeSource} disabled={isSaving}>
                  取消
                </Button>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => void handleSaveSource()}
                  loading={isSaving}
                  disabled={
                    isDeletingSource
                    || !draft.trim()
                    || draft === sourceDocument?.content
                  }
                >
                  保存
                </Button>
              </div>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={showDeleteModal} onClose={() => setShowDeleteModal(false)} title="删除资料库？">
        <div className="space-y-3">
          <p className="text-sm leading-relaxed text-ink-secondary">
            将删除 {chunkCount} 段资料，来自 {sourceCount} 个来源。
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="secondary" size="sm" onClick={() => setShowDeleteModal(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={handleDeleteCollection}
              loading={isDeleting}
              disabled={isUploading || isIndexing}
            >
              确认删除
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
};

/**
 * 一次操作之后的那一句回执。
 *
 * 它停在屏底、`slideUp` 进场、`fadeIn` 之外不动布局。**不要**把它放进正文流：那样每次上传
 * 成功整张列表都会被往下顶一格 —— 一条回执把它汇报的那个列表推开了。
 */
const StatusLine: React.FC<{
  tone: 'ok' | 'error';
  onDismiss: () => void;
  children: React.ReactNode;
}> = ({ tone, onDismiss, children }) => (
  <div className="pointer-events-none fixed inset-x-0 bottom-4 z-30 flex justify-center px-6">
    <button
      type="button"
      onClick={onDismiss}
      className={cn(
        'pointer-events-auto max-w-xl animate-slide-up rounded-card border bg-panel px-4 py-2.5',
        'text-left text-xs leading-relaxed shadow-md',
        tone === 'error' ? 'border-error/30 text-error' : 'border-stroke text-ink-secondary'
      )}
    >
      {children}
    </button>
  </div>
);
