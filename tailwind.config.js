/**
 * ProofSource theme: the original Wikisource-blue colour theme, with component shapes, spacing and
 * typography following the Wikimedia Codex guidelines (2-4px radii, 32px controls, system fonts,
 * serif headings). https://doc.wikimedia.org/codex/latest/design-tokens/overview.html
 *
 * Build:  npm install && npm run build:css   (writes static/css/app.css, which is committed)
 */
module.exports = {
  darkMode: 'class',
  content: ['./templates/**/*.html'],
  theme: {
    extend: {
      colors: {
        // ProofSource's own Wikisource-logo blues (the original colour theme), used for every accent.
        // Neutrals and status colours are Tailwind's defaults (slate / emerald / amber / rose / sky).
        ws: { dark: '#294584', chambray: '#375493', cerulean: '#236798', bright: '#006699', nature: '#4D8AC0' }
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
