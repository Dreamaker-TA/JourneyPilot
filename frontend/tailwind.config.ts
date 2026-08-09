import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    // `theme.borderRadius`（不是 `extend.borderRadius`）—— 整张默认表被**替换**掉，
    // 而不是叠加（#165）。这是故意的硬断裂：`rounded-lg` / `rounded-xl` / `rounded-2xl`
    // 与裸 `rounded` 从此都是未知类，不产出任何 CSS。
    //
    // 放在 `extend` 里会更「安全」，也正因此不能放：Tailwind 自带 sm/md/lg/xl，
    // 从 extend 里删掉这四个键，它们会**静默回落到 Tailwind 的默认值**（8/6/8/12px），
    // 于是漏改的调用点看起来还好好的，只是值变了——那正是这一轮要消灭的失败形态。
    // 现在漏改的调用点会变成直角，一眼看得出。
    borderRadius: {
      none: '0',
      label: 'var(--radius-label)', // 4px
      card: 'var(--radius-card)',   // 8px
      full: 'var(--radius-full)',
    },

    // 动效两张表也走**替换**，理由和上面 `borderRadius` 逐字相同（#18x）。
    //
    // 它们此前在 `extend` 里，于是 Tailwind 自带的那张表**一直活着**：实测 `src/` 里
    // 117 条非 `none` 的过渡类中，**56 条没写 duration、79 条没写 easing**，全部落在
    // `150ms cubic-bezier(0.4,0,0.2,1)` —— 这两个值一个都不在 token 表里（120/200/320/
    // 480/450 与三条 `--ease-*`）。表外的档位也已经在用：`duration-200`×7、`-300`×2、
    // `-500`×1。合同 §Motion 第一句写着 token 是「single source of truth」，而中间
    // 一直没有东西让它成立。
    //
    // **`DEFAULT` 那两条是这次的关键**：Tailwind v3 的 `transition-*` 工具类把
    // `theme('transitionDuration.DEFAULT')` 与 `theme('transitionTimingFunction.DEFAULT')`
    // 直接烘进每一条规则里。所以裸写 `transition-colors` 的那 56 处不用逐个改，它们
    // 现在自动落在 base + standard 上；而 `duration-200` 这类表外档位从此不产出 CSS，
    // 漏改的调用点会退回 `DEFAULT`（200ms token），不再静默用一个表外的值。
    transitionDuration: {
      DEFAULT: 'var(--dur-base)',
      fast: 'var(--dur-fast)',
      base: 'var(--dur-base)',
      slow: 'var(--dur-slow)',
      emphasis: 'var(--dur-emphasis)',
      'number-roll': 'var(--dur-number-roll)',
      loop: 'var(--dur-loop)',
    },
    transitionTimingFunction: {
      DEFAULT: 'var(--ease-standard)',
      standard: 'var(--ease-standard)',
      decelerate: 'var(--ease-decelerate)',
      accelerate: 'var(--ease-accelerate)',
    },

    // 阴影同一条（#18x）。放在 `extend` 里时 sm/md/lg 被覆盖、**xl/2xl/inner 仍然
    // 活着**，于是五处浮层用着 Tailwind 自带的 `shadow-xl`（一层冷黑 `rgb(0 0 0/.1)`），
    // 而 §Color 一共十二个命名色、`--shadow-*` 三档全是暖灰 `rgba(42,38,26,…)`。
    // 换成替换后 `shadow-xl` 不产出 CSS，多出来的一档会立刻看得出来。
    boxShadow: {
      none: 'none',
      sm: 'var(--shadow-sm)',
      md: 'var(--shadow-md)',
      lg: 'var(--shadow-lg)',
    },

    extend: {
      colors: {
        // rgb(var(--*-rgb) / <alpha-value>) so opacity modifiers (bg-accent/10 …)
        // compile; the -rgb triplets live in src/index.css :root.
        bg: 'rgb(var(--color-bg-rgb) / <alpha-value>)',
        panel: 'rgb(var(--color-panel-rgb) / <alpha-value>)',
        surface: 'rgb(var(--color-surface-rgb) / <alpha-value>)',
        'surface-soft': 'rgb(var(--color-surface-soft-rgb) / <alpha-value>)',
        highlight: 'rgb(var(--color-highlight-rgb) / <alpha-value>)',
        stroke: 'rgb(var(--color-stroke-rgb) / <alpha-value>)',
        ink: {
          DEFAULT: 'rgb(var(--color-ink-rgb) / <alpha-value>)',
          secondary: 'rgb(var(--color-ink-secondary-rgb) / <alpha-value>)',
          muted: 'rgb(var(--color-ink-muted-rgb) / <alpha-value>)',
        },
        accent: {
          DEFAULT: 'rgb(var(--color-accent-rgb) / <alpha-value>)',
          hover: 'rgb(var(--color-accent-hover-rgb) / <alpha-value>)',
          soft: 'rgb(var(--color-accent-soft-rgb) / <alpha-value>)',
        },
        // 压在 accent 实底上的字色（#247–#255）。纸面上是白（5.86），深墨底板上是近黑
        // （8.98）—— 主按钮此前写死 `text-white`，那只字面色在底板上掉到 1.98，
        // 而写死的颜色的定义就是「不跟着换档」。
        'on-accent': 'rgb(var(--color-on-accent-rgb) / <alpha-value>)',
        success: 'rgb(var(--color-success-rgb) / <alpha-value>)',
        error: 'rgb(var(--color-error-rgb) / <alpha-value>)',
        warning: 'rgb(var(--color-warning-rgb) / <alpha-value>)',
        chart: 'rgb(var(--color-chart-rgb) / <alpha-value>)',
        vermilion: 'rgb(var(--color-vermilion-rgb) / <alpha-value>)',
        navy: 'rgb(var(--color-navy-rgb) / <alpha-value>)',
        // Glass tokens carry their own baked-in alpha — no modifier support.
      },
      opacity: {
        // Off-scale steps already written in TSX (bg-ink/8, bg-warning/12, border-accent/18)
        8: '0.08',
        12: '0.12',
        18: '0.18',
      },
      // 字形由仓里的文件负责，不由用户机器决定（#205）。文件与理由见
      // `src/assets/fonts/fonts.css`；这里只写「谁排在谁前面」。
      //
      // 拉丁在前、汉字在后是**分工**而不是兜底：Inter 的 latin 子集只覆盖拉丁范围，
      // 汉字与全角标点根本不在它的字符表里，所以逐字匹配时自然落到 Noto Sans SC。
      // 反过来写（汉字家族在前）会让数字和字母也由 Noto 的拉丁部分渲染 —— 小字号下比
      // Inter 软，而产品的读数全是小字号。
      fontFamily: {
        sans: ['"Inter Variable"', '"Noto Sans SC Variable"', 'system-ui', 'sans-serif'],
        // 唯一还随机器变的一只（用户本轮只要了拉丁+汉字两只）。读数（耗时/价格/坐标）
        // 都在这上面，所以任何与读数对齐有关的修法都不许依赖字体度量 —— 见 #203。
        mono: ['"SF Mono"', 'Monaco', 'Consolas', '"Courier New"', 'monospace'],
        // 子集在前（14 字，3.9 KB，首屏即到），全量在后（子集之外的字）。
        // `Songti SC` 走了：它是系统字体，而多数机器上没有 —— 它在这张表里的作用
        // 一直是「让缺字看起来有人管」。
        display: ['"Noto Serif SC Subset"', '"Noto Serif SC Variable"', 'serif'],
      },
      animation: {
        'fade-in': 'fadeIn var(--dur-slow) var(--ease-decelerate)',
        'slide-up': 'slideUp var(--dur-slow) var(--ease-decelerate)',
        'slide-in-right': 'slideInRight var(--dur-slow) var(--ease-decelerate)',
      },
      keyframes: {
        fadeIn: {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        slideUp: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        slideInRight: {
          from: { opacity: '0', transform: 'translateX(20px)' },
          to: { opacity: '1', transform: 'translateX(0)' },
        },
      },
    },
  },
  plugins: [],
} satisfies Config
