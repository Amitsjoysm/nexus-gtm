---
name: build-a-list
description: Build a target list of accounts or contacts in InfoJoy GTM, for example companies like a best customer, and optionally add the people to a campaign for review. Use when the user asks for lookalikes, a prospect list, more companies like an account, or to load people into a campaign.
---

Build a saved list the user can work from, using the InfoJoy GTM connector.

1. Find the seed account with `search`. Confirm it with the user if the name is ambiguous.
2. Ask how many companies they want (10 is a good default), then call `find_similar_companies`. It spends credits per company returned and adds nothing by itself.
3. Show the candidates with their fit score and one line on why they match. Let the user choose.
4. For each company the user keeps, call `create_account` with its name and website domain. The domain matters: it is how InfoJoy GTM tells companies apart and collects their signals.
5. Call `create_list` (kind `account`) with a clear name, then `add_to_list` with the new account ids. For people, use a `contact` list and `contact_ids`.
6. If the user wants outreach, call `list_campaigns` to choose a campaign, then `add_contacts_to_campaign` with the list. Nothing is sent: drafts wait for the user's review in InfoJoy GTM.

Report what was created with links when the tools return them. Ask before each step that spends credits, and stop paid steps if `get_credit_balance` shows the balance is too low.
