// Flat config written against the versions this project actually installs.
//
// The previous revision used three ESLint-9-only APIs — `eslint/config`
// (defineConfig/globalIgnores), flat `extends`, and
// `eslint-plugin-react-hooks`'s `configs.flat` preset (plugin v5+) — while
// package.json pins eslint ^8.57 and react-hooks ^4.6. The result was that
// `npm run lint` had not run at all: it died on ERR_PACKAGE_PATH_NOT_EXPORTED
// before linting a single file.
//
// ESLint 8.57 does understand flat config; it just lacks those helpers. So the
// same rules are expressed with the plain array form and the react-hooks rules
// wired explicitly. No dependency changes, no rule changes.
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'

export default [
  { ignores: ['dist/**', 'node_modules/**'] },
  {
    // Build-time files run under Node, not in the browser: without these
    // globals every `process.env` read in vite.config.js is a no-undef error.
    files: ['*.config.js', 'vite-plugin-seo.js', 'seo/**/*.js'],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...js.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
    },
  },
]
