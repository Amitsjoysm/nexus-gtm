/**
 * Inline SVG icon set. Stroke-based, inherit currentColor, 24×24 viewBox.
 * Decorative by default (aria-hidden); wrap in a labelled control for meaning.
 */
import type { SVGProps } from "react";

function Svg(props: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      // Intrinsic size: an SVG with only a viewBox stretches to its container's width,
      // so any usage the page CSS forgot to size rendered as a giant full-width "logo"
      // (seen on the Inbox suggestion row and the Alerts channel icon). 1em pins every
      // icon to the surrounding text size by default; explicit CSS still overrides.
      width="1em"
      height="1em"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    />
  );
}

export const LogoMark = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M5 19V5l14 14V5" />
  </Svg>
);

export const DashboardIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <rect x="3" y="3" width="7" height="9" rx="1.5" />
    <rect x="14" y="3" width="7" height="5" rx="1.5" />
    <rect x="14" y="12" width="7" height="9" rx="1.5" />
    <rect x="3" y="16" width="7" height="5" rx="1.5" />
  </Svg>
);

export const InboxIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M3 12h5l2 3h4l2-3h5" />
    <path d="M5.5 5h13l2.5 7v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5z" />
  </Svg>
);

export const PhoneIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z" />
  </Svg>
);

export const BuildingIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M4 21V5a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v16" />
    <path d="M15 9h3a2 2 0 0 1 2 2v10" />
    <path d="M2 21h20M8 7h3M8 11h3M8 15h3" />
  </Svg>
);

export const SignalIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M3 12h3l3 7 4-14 3 7h5" />
  </Svg>
);

export const BellIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6Z" />
    <path d="M10.5 19a1.5 1.5 0 0 0 3 0" />
  </Svg>
);

export const UsersIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
    <path d="M16 5.2a3.2 3.2 0 0 1 0 5.6M17.5 19a5.5 5.5 0 0 0-2.4-4.5" />
  </Svg>
);

export const SettingsIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M22 12h-3M5 12H2M19 5l-2 2M7 17l-2 2M19 19l-2-2M7 7 5 5" />
  </Svg>
);

export const SearchIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.2-3.2" />
  </Svg>
);

export const PlusIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M12 5v14M5 12h14" />
  </Svg>
);

export const CheckIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M20 6 9 17l-5-5" />
  </Svg>
);

export const XIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M18 6 6 18M6 6l12 12" />
  </Svg>
);

export const ChevronRightIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="m9 6 6 6-6 6" />
  </Svg>
);

export const ChevronLeftIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="m15 6-6 6 6 6" />
  </Svg>
);

export const RefreshIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5" />
  </Svg>
);

export const SparklesIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6L12 3Z" />
    <path d="M19 14l.7 1.8L21.5 16.5 19.7 17.2 19 19l-.7-1.8L16.5 16.5l1.8-.7L19 14Z" />
  </Svg>
);

export const SendIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7Z" />
  </Svg>
);

export const SunIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5 4 4M20 20l-1-1M19 5l1-1M4 20l1-1" />
  </Svg>
);

export const MoonIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
  </Svg>
);

export const LogOutIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3" />
    <path d="M10 17l-5-5 5-5M5 12h11" />
  </Svg>
);

export const MenuIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M3 6h18M3 12h18M3 18h18" />
  </Svg>
);

export const ExternalLinkIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M14 4h6v6M20 4l-9 9M19 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h4" />
  </Svg>
);

export const AlertTriangleIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M12 3 2.5 19.5h19L12 3Z" />
    <path d="M12 9v5M12 17.5h.01" />
  </Svg>
);

export const TargetIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="12" cy="12" r="8.5" />
    <circle cx="12" cy="12" r="4.5" />
    <circle cx="12" cy="12" r="1" />
  </Svg>
);

export const TrendUpIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M3 17l6-6 4 4 8-8M21 7v5h-5" />
  </Svg>
);

export const ListIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" />
  </Svg>
);

export const BoltIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />
  </Svg>
);

export const CreditCardIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <rect x="2" y="5" width="20" height="14" rx="2" />
    <path d="M2 10h20" />
  </Svg>
);

export const PlugIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M9 2v6M15 2v6M7 8h10v3a5 5 0 0 1-10 0V8ZM12 16v6" />
  </Svg>
);

export const MessageIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-4 4V6a1 1 0 0 1 1-1Z" />
  </Svg>
);

export const MailIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <path d="m3 7 9 6 9-6" />
  </Svg>
);

export const FileTextIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5M9 13h6M9 17h6" />
  </Svg>
);

export const HelpCircleIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="12" cy="12" r="9" />
    <path d="M9.2 9a2.8 2.8 0 0 1 5.4 1c0 1.8-2.6 2.4-2.6 4M12 17.5h.01" />
  </Svg>
);

export const UserCheckIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="9" cy="8" r="3.4" />
    <path d="M3.5 19a5.5 5.5 0 0 1 11 0M16 12l2 2 4-4" />
  </Svg>
);

export const TrashIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13M10 11v6M14 11v6" />
  </Svg>
);

export const PhoneSearchIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M4 5a1 1 0 0 1 1-1h2.3a1 1 0 0 1 1 .8l.6 2.6a1 1 0 0 1-.3 1L7.2 9.7a12 12 0 0 0 5 5l1.3-1.4a1 1 0 0 1 1-.3l2.6.6a1 1 0 0 1 .8 1V17a1 1 0 0 1-1 1A13 13 0 0 1 4 5Z" />
    <circle cx="17" cy="7" r="2.5" />
    <path d="M19 9l2 2" />
  </Svg>
);

export const DownloadIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M12 3v12M7 11l5 5 5-5M5 20h14" />
  </Svg>
);

export const UploadIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M12 17V5M7 9l5-5 5 5M5 20h14" />
  </Svg>
);

export const WorkflowIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="5" cy="6" r="2.5" />
    <circle cx="5" cy="18" r="2.5" />
    <circle cx="19" cy="12" r="2.5" />
    <path d="M7.5 6h6a3 3 0 0 1 3 3v0M7.5 18h6a3 3 0 0 0 3-3v0" />
  </Svg>
);

export const ShieldCheckIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6l-7-3Z" />
    <path d="m9 12 2 2 4-4" />
  </Svg>
);

export const ActivityIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M3 12h3.5l2.5-7 4 14 2.5-7H21" />
  </Svg>
);

export const LockIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <rect x="4.5" y="10.5" width="15" height="9.5" rx="2" />
    <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" />
  </Svg>
);

export const TrophyIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <path d="M7 4h10v4a5 5 0 0 1-10 0V4Z" />
    <path d="M17 5h2.5a1.5 1.5 0 0 1 0 5H17M7 5H4.5a1.5 1.5 0 0 0 0 5H7" />
    <path d="M12 13v3M9 20h6M10 16h4l.5 4h-5l.5-4Z" />
  </Svg>
);

export const NetworkIcon = (props: SVGProps<SVGSVGElement>) => (
  <Svg {...props}>
    <circle cx="6" cy="6" r="2.3" />
    <circle cx="18" cy="6" r="2.3" />
    <circle cx="12" cy="18" r="2.3" />
    <path d="M8 6.4h8M7.4 7.9 10.8 16M16.6 7.9 13.2 16" />
  </Svg>
);
