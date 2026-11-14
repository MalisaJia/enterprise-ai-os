import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { initTheme } from './theme'
import './styles.css'

// Resolve data-theme before first React paint (index.html already applied a
// best-effort early value; this is the canonical resolution + parent follow).
initTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
