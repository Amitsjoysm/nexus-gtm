import type { SVGProps } from "react";

/**
 * Mailbox-provider marks for the contact list.
 *
 * An SDR scanning a list of 200 contacts is asking one question — "is this a Google shop or a
 * Microsoft shop?" — because it changes the deliverability rules, the sending window, and how the
 * thread will render on the other side. The previous badge answered it with a coloured letter
 * (`G`, `M`, `O`), which needs reading rather than recognising, and `M` for Microsoft 365 sat one
 * letter away from a Gmail `G` at 9px.
 *
 * These are **recognised, not read**: shape and brand colour do the work, so the answer arrives in
 * peripheral vision while the eye is on the address.
 *
 * Deliberately hand-drawn inline SVG:
 *
 * * A strict CSP and the design system both rule out fetching a logo from a CDN, and favicon
 *   services (`google.com/s2/favicons`, DuckDuckGo's icon proxy) would leak every prospect's mail
 *   domain to a third party on every page render — a privacy problem, not just a dependency one.
 * * Simplified geometry rather than pixel-exact logos: these identify the service (nominative use)
 *   at 16px, where exact paths are indistinguishable anyway.
 *
 * `currentColor` is avoided on purpose — brand colour IS the signal. The generic marks
 * (custom/disposable) do use theme tokens, because they are states rather than brands.
 */

type IconProps = SVGProps<SVGSVGElement>;

const base = (props: IconProps) => ({
  width: 16,
  height: 16,
  viewBox: "0 0 16 16",
  xmlns: "http://www.w3.org/2000/svg",
  focusable: "false" as const,
  // The badge wrapper carries the label; the mark itself must not be announced twice.
  "aria-hidden": true,
  ...props,
});

/** Gmail / Google Workspace — the envelope with the four-colour M. */
export const GmailIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect x="1" y="3" width="14" height="10" rx="1.6" fill="#fff" />
    <path d="M1 4.6 8 9.4l7-4.8V4.6A1.6 1.6 0 0 0 13.4 3H2.6A1.6 1.6 0 0 0 1 4.6Z" fill="#ea4335" />
    <path d="M1 4.6v7.8A1.6 1.6 0 0 0 2.6 14H4V7.2L1 4.6Z" fill="#4285f4" />
    <path d="M15 4.6v7.8A1.6 1.6 0 0 1 13.4 14H12V7.2l3-2.6Z" fill="#34a853" />
    <path d="M4 14V7.2l4 2.9 4-2.9V14H4Z" fill="#fbbc04" />
  </svg>
);

/** Microsoft 365 — the four-square logo. Unmistakable against Gmail's envelope. */
export const MicrosoftIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect x="1.5" y="1.5" width="6" height="6" fill="#f25022" />
    <rect x="8.5" y="1.5" width="6" height="6" fill="#7fba00" />
    <rect x="1.5" y="8.5" width="6" height="6" fill="#00a4ef" />
    <rect x="8.5" y="8.5" width="6" height="6" fill="#ffb900" />
  </svg>
);

/** Outlook.com — the blue envelope with the O. Distinct from Microsoft 365 on purpose: one is a
 *  consumer mailbox and the other a corporate tenant, which is a real difference to an SDR. */
export const OutlookIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect x="1" y="3" width="14" height="10" rx="1.6" fill="#0f6cbd" />
    <rect x="7.4" y="4.6" width="6.4" height="6.8" rx="0.8" fill="#fff" opacity="0.9" />
    <ellipse cx="5" cy="8" rx="2.9" ry="3.4" fill="#fff" />
    <ellipse cx="5" cy="8" rx="1.4" ry="1.9" fill="#0f6cbd" />
  </svg>
);

/** Yahoo Mail. */
export const YahooIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect width="16" height="16" rx="3.2" fill="#6001d2" />
    <path d="M3.4 4.4h2.2L8 8.1l2.4-3.7h2.2L9 9.6V12H7V9.6L3.4 4.4Z" fill="#fff" />
  </svg>
);

/** Proton Mail — the shield. */
export const ProtonIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <path d="M8 1.4 14 3.6v4.2c0 3.4-2.4 6-6 6.8-3.6-.8-6-3.4-6-6.8V3.6L8 1.4Z" fill="#6d4aff" />
    <path d="M4.6 6.4h6.8v1.1L8 9.9 4.6 7.5V6.4Z" fill="#fff" />
  </svg>
);

/** Zoho Mail. */
export const ZohoIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect width="16" height="16" rx="3.2" fill="#e42527" />
    <path d="M4 4.6h8v1.5l-5.3 4.4H12V12H4v-1.5l5.3-4.4H4V4.6Z" fill="#fff" />
  </svg>
);

/** iCloud Mail. */
export const AppleMailIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect width="16" height="16" rx="3.2" fill="#3693f3" />
    <path
      d="M11.6 7.3a2.9 2.9 0 0 0-2.2-1.4c-.9-.1-1.3.3-2 .3s-1.2-.4-2-.3a2.9 2.9 0 0 0-2.4 3.4c.3 1.4 1.3 2.9 2.1 2.9.6 0 .9-.4 1.7-.4s1 .4 1.7.4c.8 0 1.6-1.3 2-2.3a2.5 2.5 0 0 1-1-2.1c.1-.2.1-.4.1-.5Z"
      fill="#fff"
    />
  </svg>
);

/**
 * A self-hosted or unrecognised mail domain. Theme tokens, not a brand colour — "we could not
 * identify this" is a state, and dressing it as a brand would imply we know something we do not.
 */
export const CustomMailIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <rect
      x="1.5" y="3.5" width="13" height="9" rx="1.6"
      fill="none" stroke="var(--text-subtle, #8a8f98)" strokeWidth="1.4"
    />
    <path
      d="m2.4 4.8 5.6 4 5.6-4"
      fill="none" stroke="var(--text-subtle, #8a8f98)" strokeWidth="1.4"
      strokeLinecap="round" strokeLinejoin="round"
    />
  </svg>
);

/** A throwaway-address domain. The one mark that is a WARNING rather than an identification. */
export const DisposableMailIcon = (props: IconProps) => (
  <svg {...base(props)}>
    <path d="M8 1.8 15 14H1L8 1.8Z" fill="var(--danger, #c0392b)" />
    <rect x="7.2" y="6" width="1.6" height="4.2" rx="0.8" fill="#fff" />
    <rect x="7.2" y="11" width="1.6" height="1.6" rx="0.8" fill="#fff" />
  </svg>
);

/** Backend `email_provider` value → mark. Anything unlisted falls back to the generic envelope. */
export const PROVIDER_ICONS: Record<string, (p: IconProps) => JSX.Element> = {
  gsuite: GmailIcon,
  google: GmailIcon,
  gmail: GmailIcon,
  office365: MicrosoftIcon,
  microsoft: MicrosoftIcon,
  outlook: OutlookIcon,
  hotmail: OutlookIcon,
  yahoo: YahooIcon,
  proton: ProtonIcon,
  protonmail: ProtonIcon,
  zoho: ZohoIcon,
  icloud: AppleMailIcon,
  apple: AppleMailIcon,
  custom: CustomMailIcon,
  disposable: DisposableMailIcon,
};

export function ProviderIcon({ provider, ...props }: IconProps & { provider?: string | null }) {
  const Mark = PROVIDER_ICONS[(provider || "").toLowerCase()] ?? CustomMailIcon;
  return <Mark {...props} />;
}
