import type { ReactNode } from 'react'

interface Props {
  title: string
  body: ReactNode
  confirmText: string
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
}

/** Minimal modal with explicit confirm — used for sandbox mode switches. */
export default function ConfirmDialog({
  title,
  body,
  confirmText,
  danger,
  onConfirm,
  onCancel,
}: Props) {
  return (
    <div className="modal-veil" onClick={onCancel} role="presentation">
      <div
        className="modal rise"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">{title}</div>
        <div className="modal-body">{body}</div>
        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button
            className={`btn ${danger ? 'btn-danger' : 'btn-solid'}`}
            onClick={onConfirm}
            autoFocus
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  )
}
