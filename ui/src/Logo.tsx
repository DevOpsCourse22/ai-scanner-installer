/** The app's mark (three lines of code, the middle one highlighted). On first
    paint the highlighter sweeps across the middle line, once. */
export default function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" className="logo">
      <rect width="64" height="64" rx="15" fill="var(--brand)" />
      <rect x="12" y="12" width="30" height="7" rx="3.5" fill="var(--brand-ink)" />
      <path className="logo-mark" d="M8 26.5 L56 23.5 L56.5 37.5 L8.5 40.5 Z" fill="var(--marker)" />
      <rect x="13" y="28.5" width="36" height="7" rx="3.5" fill="var(--brand)" />
      <rect x="12" y="45" width="21" height="7" rx="3.5" fill="var(--brand-ink)" opacity=".72" />
    </svg>
  );
}
