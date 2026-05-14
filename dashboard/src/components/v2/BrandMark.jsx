/**
 * BrandMark — rounded-square mark with an "X" glyph on solid brand
 * orange. Used in the top-left of the header.
 */
export default function BrandMark({ size = 28, className = '' }) {
  return (
    <span
      className={`inline-flex items-center justify-center shrink-0 ${className}`}
      style={{
        width: size,
        height: size,
        borderRadius: 7,
        background: 'var(--brand-orange)',
        boxShadow:
          '0 0 0 1px rgba(255,255,255,0.06), 0 1px 2px rgba(0,0,0,0.6)',
      }}
      aria-hidden="true"
    >
      <svg
        viewBox="0 0 24 24"
        width={Math.round(size * 0.55)}
        height={Math.round(size * 0.55)}
        fill="none"
        stroke="#0F0F0F"
        strokeWidth="2.5"
        strokeLinecap="round"
      >
        <path d="M5 5L19 19M19 5L5 19" />
      </svg>
    </span>
  )
}
