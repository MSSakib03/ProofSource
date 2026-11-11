/**
 * ProofSource theme = Wikimedia Codex design tokens mapped onto Tailwind's palette names, so the
 * whole template follows the Wikimedia design style guide (light and night mode) without a
 * colour being hard-coded in the markup. Token reference:
 * https://doc.wikimedia.org/codex/latest/design-tokens/overview.html
 *
 * Build:  npm install && npm run build:css   (writes static/css/app.css, which is committed)
 */
module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        // Codex neutrals (base100 … base10, night-mode backgrounds)
        slate: {
          50: '#f8f9fa', 100: '#eaecf0', 200: '#dadde3', 300: '#c8ccd1', 400: '#a2a9b1', 500: '#72777d',
          600: '#54595d', 700: '#404244', 800: '#27292d', 900: '#202122', 950: '#101418'
        },
        // Codex "progressive" blue (#36c) and its states; `nature` is the night-mode progressive
        ws: { dark: '#233566', chambray: '#3056a9', cerulean: '#3366cc', bright: '#3366cc', nature: '#88a3e8' },
        // Codex success / destructive / warning families
        emerald: {
          50: '#effaf6', 100: '#dff2eb', 200: '#b2e3d0', 300: '#6dcbab', 400: '#2cb491', 500: '#099979',
          600: '#177860', 700: '#0d6b55', 800: '#0a5242', 900: '#132821', 950: '#0b1c17'
        },
        rose: {
          50: '#fff4f2', 100: '#ffe9e5', 200: '#ffc8bd', 300: '#fd9c8b', 400: '#fd7865', 500: '#f54739',
          600: '#bf3c2c', 700: '#9f3526', 800: '#7a2a1f', 900: '#3c1a13', 950: '#2a120d'
        },
        amber: {
          50: '#fef6e7', 100: '#fdf2d5', 200: '#fce3a6', 300: '#f5c95c', 400: '#ca982e', 500: '#ab7f2a',
          600: '#886425', 700: '#6e5020', 800: '#4f3a17', 900: '#2d2212', 950: '#1f170c'
        },
        orange: {
          50: '#fff3e8', 100: '#ffe4cc', 200: '#ffc999', 300: '#ffad66', 400: '#f2923a', 500: '#e57716',
          600: '#b85f10', 700: '#8f4a0c', 800: '#663509', 900: '#331a04', 950: '#1f1003'
        },
        sky: { 50: '#f1f4fd', 100: '#e8eeff', 300: '#a6bbf5', 400: '#88a3e8', 500: '#3366cc', 600: '#3056a9', 700: '#233566' },
        // ProofreadPage page-quality colours (the colours every Wikisource editor knows)
        quality: { none: '#ffa0a0', problem: '#b0b0ff', proofread: '#ffe867', validated: '#90ff90', empty: '#dddddd' }
      },
      borderRadius: {
        DEFAULT: '2px', sm: '2px', md: '2px', lg: '4px', xl: '6px', '2xl': '8px'
      },
      boxShadow: {
        sm: '0 1px 1px 0 rgba(0,0,0,.06)',
        DEFAULT: '0 1px 2px 0 rgba(0,0,0,.08)',
        md: '0 2px 2px 0 rgba(0,0,0,.12)',
        lg: '0 4px 12px 0 rgba(0,0,0,.12)',
        xl: '0 12px 32px 0 rgba(0,0,0,.18)'
      },
      fontFamily: {
        sans: ['-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'Inter', 'Lato', '"Noto Sans"',
               '"Noto Sans Bengali"', '"Nirmala UI"', 'Vrinda', 'Helvetica', 'Arial', 'sans-serif'],
        serif: ['"Linux Libertine"', '"Georgia"', '"Times"', '"Source Serif Pro"', '"Noto Serif Bengali"',
                '"Noto Serif"', 'serif'],
        mono: ['Menlo', 'Consolas', '"Liberation Mono"', '"Courier New"', 'monospace']
      }
    }
  },
  plugins: []
};
