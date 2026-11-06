# The demo video (required by ChatGPT, useful everywhere)

OpenAI requires a reviewer-accessible video URL (`review.demo_recording_url`); pass it to
`python scripts/build.py --demo-url <url>` in the listing repository or enter it in the portal's
Review details. Anthropic and Microsoft do not require one, but the same recording supports their
reviews and gives the screenshots.

- About one minute, no voice needed, captions welcome. Record in ChatGPT; a second take in Claude
  gives the screenshots for Anthropic.
- Use the reviewer workspace (`docs/mcp/reviewer-workspace.md`), never a customer's.
- Host it where a reviewer can open it without signing in (an unlisted video, or a public file
  link). It must stay up while the plugin is listed.
- Show nothing secret: no API keys, no other workspaces, no real prospects' personal emails beyond
  what the reviewer workspace holds.

## Shots

1. **Connect**: the plugin or connector, Connect, the InfoJoy GTM sign-in, the consent screen
   with Read, Research and Write ticked, Allow access. (10 s)
2. **"What should I work on today?"**: the prioritised list with reasons. (10 s)
3. **"Research Gong and tell me why they might buy now."**: the credit notice, then the brief.
   (10 s)
4. **"Find contacts at Vanta and verify their emails."**: contacts with verification results.
   (10 s)
5. **"Draft an email to the first Vanta contact and save it to my drafts."**: the draft, "saved,
   not sent", then the email in the mailbox's Drafts folder. (15 s)
6. **"Make a campaign draft for my list Reviewer targets."**: the draft, then the campaign in
   InfoJoy GTM waiting for review. (10 s)
7. **A refusal**: "Email the first Vanta contact right now and send it." and the explanation.
   (5 s)

## Screenshots

At least three PNGs, 1000 px wide or more, cropped to the assistant's answer: the daily
priorities, the research brief, and the saved draft. Keep the prompt text for each beside it;
Anthropic asks for it separately when a listing has carousel images.
