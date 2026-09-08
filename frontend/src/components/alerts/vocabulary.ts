import type { AlertChannelKind } from "@/lib/types";

/**
 * The words the alert screens use, in one place.
 *
 * Both the guided setup in Settings and the connections panel on Integrations name the same
 * channels, and a second copy of "Teams" spelled differently is how two screens start describing
 * one credential as two things. The server owns the vocabulary itself (categories are derived from
 * the alert rules, channels from the live registry); this only owns how to say it.
 */

/** Categories, human-labelled. An unknown key falls through to a humanised form of itself, so a
 *  category added server-side appears rather than vanishing. */
export const CATEGORY_LABEL: Record<string, string> = {
  funding: "Funding round",
  hiring: "Hiring and leadership",
  champion: "Champion changed jobs",
  intent: "Buying intent",
  technographic: "Tech stack change",
  product: "Product or pricing change",
  news: "Press mention",
  activity: "Account activity",
  usage: "Product usage",
};

/** What actually fires each one. The dropdown is the first place anybody meets these names, and
 *  "technographic" means nothing on its own. */
export const CATEGORY_BLURB: Record<string, string> = {
  funding: "A round is announced. Budget is unlocked and next year's priorities are being set.",
  hiring: "Open roles or a leadership hire. Headcount names the team with money to spend.",
  champion: "Someone you know moved to a new company. A warm opening at a brand new account.",
  intent: "They compared vendors on a review site or visited your pricing page.",
  technographic: "They added or dropped a technology your product sits next to.",
  product: "Their pricing, security or careers page changed. A live strategy shift.",
  news: "Any other press coverage naming the account.",
  activity: "Calls and touches logged against the account by your team.",
  usage: "Product usage moved — often a limit being hit.",
};

/** Every channel the delivery vocabulary can name, including the two nobody connects. */
export const CHANNEL_LABEL: Record<string, string> = {
  in_app: "In the app",
  email: "Email",
  slack: "Slack",
  teams: "Microsoft Teams",
  telegram: "Telegram",
  webhook: "Webhook",
};

/**
 * Channels a workspace connects a credential for.
 *
 * `in_app` is always available and needs nothing. `webhook` is deliberately absent: it stays a
 * deployment-level integration an operator wires, not an account a rep connects, so offering a
 * connect form for it would promise something this screen cannot deliver.
 */
export const CONNECTABLE_CHANNELS: readonly AlertChannelKind[] = [
  "slack",
  "teams",
  "telegram",
  "email",
] as const;

export function isConnectable(channel: string): channel is AlertChannelKind {
  return (CONNECTABLE_CHANNELS as readonly string[]).includes(channel);
}

/** One sentence saying what connecting this channel does, and where the credential comes from.
 *  Every one of these is a real menu path — "create a webhook" is not an instruction anybody can
 *  follow without knowing where. */
export const CHANNEL_HELP: Record<AlertChannelKind, string> = {
  slack:
    "In Slack: Apps → Incoming Webhooks → Add to Slack, pick the channel, then copy the webhook URL.",
  teams:
    "In Teams: right-click the channel → Connectors → Incoming Webhook → Create, then copy the URL.",
  telegram:
    "Message @BotFather to create a bot and copy its token, then add the bot to your group and use that group's chat id.",
  email: "Alerts are sent from your workspace's configured sender to the address you give here.",
};

/** Per-field label, placeholder and input type. Driven by the field names the SERVER declares for
 *  each channel, so a channel that starts needing a second field cannot leave the form behind. */
export const FIELD_META: Record<
  string,
  { label: string; placeholder: string; type: "text" | "url" | "email" | "password" }
> = {
  url: {
    label: "Webhook URL",
    placeholder: "https://hooks.slack.com/services/…",
    type: "url",
  },
  bot_token: {
    label: "Bot token",
    placeholder: "123456789:AA…",
    type: "password",
  },
  chat_id: {
    label: "Chat ID",
    placeholder: "-1001234567890",
    type: "text",
  },
  to: {
    label: "Send alerts to",
    placeholder: "alerts@yourcompany.com",
    type: "email",
  },
};

export function fieldMeta(name: string) {
  return (
    FIELD_META[name] ?? {
      label: name.replace(/[._]/g, " "),
      placeholder: "",
      type: "text" as const,
    }
  );
}

/** Look a name up, falling back to a readable form of the key rather than showing nothing. */
export function label(map: Record<string, string>, key: string): string {
  return map[key] ?? key.replace(/[._]/g, " ");
}
