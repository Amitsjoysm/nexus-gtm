# The reviewer workspace for directory submissions

Anthropic, OpenAI and Microsoft each test the server with an account we give them. The same
account serves all three. The owner creates it on **production** (`gtm.infojoy.com`); nothing in
this file is a credential, and none may be written here or in any ticket, commit or chat:
credentials go only into each portal's private reviewer field.

## The account

- A dedicated workspace, for example "InfoJoy GTM Review", owned by a dedicated mailbox you
  control (a reviewer may need password reset to work).
- **No MFA** on the owner, and no magic-link or OTP-only sign-in for it: reviewers cannot receive a
  code (OpenAI states this outright). Password sign-in only.
- Role: owner, so every tool is visible when they tick all three permissions.
- A plan with Prospector and campaigns, and **credits**: at least 500 (each review runs the five
  positive cases, several times).
- Reachable from the public internet; no IP allowlist or VPN in front of the app.

## Data the test cases expect

The five positive test cases in `distribution/openai/plugin.json` (reused for Anthropic and
Microsoft) name these records. Create them by hand or through the app, never with fabricated
signals:

| Record | Why |
|---|---|
| Accounts **Gong** (`gong.io`) and **Vanta** (`vanta.com`), plus three or four more ICP-fit companies | "Research Gong", "Find contacts at Vanta". Let the refresh run so each has real signals. |
| An ICP on the Relevance page | fit scores, `add_icp_companies` |
| At least one contact at Vanta with a verified email | "Draft an email to the first Vanta contact" |
| Open inbox tasks and recent signals | "What should I work on today?" |
| A contact list named **Reviewer targets** with three or more people | "Make a campaign draft for my list Reviewer targets" |
| A **connected mailbox** (Settings, My mailboxes) on a mailbox you own | `save_email_draft`, `save_reply_draft`, campaign drafts |
| One reply on the reply desk, one queued call | `list_queue`, `draft_reply` |

Run every positive and negative case yourself in Claude and ChatGPT before submitting, with all
three permissions ticked, and note anything that answers differently from `expected_behavior`.

## Instructions to paste into each portal's test field

> 1. Open the connector and choose Connect. You are sent to gtm.infojoy.com.
> 2. Sign in with the email and password given in this form.
> 3. On the consent screen tick Read, Research and Write, then Allow access.
> 4. Try the prompts below. Research tools spend this workspace's credits, which are provided.
>    Nothing can send email, launch a campaign, place a call or delete a record; those prompts
>    should be refused.

Then the prompts from `review.test_cases`.

## After review

Keep the workspace. Directories re-test on updates (OpenAI rescans tools daily, Anthropic
re-checks new plugin versions, Microsoft re-validates resubmissions). Rotate its password after
each submission round and update the portals' private fields.
