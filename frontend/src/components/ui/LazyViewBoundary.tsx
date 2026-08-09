import React from 'react';
import { Button } from './Button';

/**
 * 懒加载视图的最小错误边界（懒加载分包）。
 *
 * 五个非 chat 视图与地图子树走 React.lazy 后，chunk 下载可能因网络异常失败。
 * 无边界时 React 会抛错到根、整屏白屏；本边界兜住该失败，就地显示一句完整文案
 * 与「重新加载」Button——点击整页刷新重新拉取 chunk。刻意**不做重试基建**
 * （无指数退避、无 chunk 级 import 重试）：一次直白的整页刷新是最诚实的恢复。
 *
 * 错误边界必须是 class 组件（React 只在 class 上暴露 getDerivedStateFromError /
 * componentDidCatch）。这是全站唯一获准的 class 组件用途。
 */
interface LazyViewBoundaryProps {
  children: React.ReactNode;
  resetKey?: React.Key;
}

interface LazyViewBoundaryState {
  failed: boolean;
}

export class LazyViewBoundary extends React.Component<
  LazyViewBoundaryProps,
  LazyViewBoundaryState
> {
  state: LazyViewBoundaryState = { failed: false };

  static getDerivedStateFromError(): LazyViewBoundaryState {
    return { failed: true };
  }

  componentDidUpdate(prevProps: LazyViewBoundaryProps) {
    if (this.state.failed && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  private handleReload = () => {
    window.location.reload();
  };

  render() {
    if (this.state.failed) {
      return (
        <div className="flex h-full items-center justify-center bg-bg p-6">
          <div className="max-w-[52ch] rounded-card border border-stroke bg-panel p-6 text-center shadow-sm">
            <p className="text-sm leading-relaxed text-ink-secondary">
              这个页面没能加载出来，通常是网络中断导致的。
            </p>
            <div className="mt-4 flex justify-center">
              <Button variant="secondary" size="sm" onClick={this.handleReload}>
                重新加载
              </Button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
