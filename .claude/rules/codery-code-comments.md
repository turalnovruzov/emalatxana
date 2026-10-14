<!-- template: product-owner @ 0.2.0 — shared; change it through Codery -->

# Comments in the code you write

One rule covers every comment you add, in production code and in test code:

**A comment earns its place only by saying something the code cannot say itself.**

Code that explains itself line by line is harder to read than the code alone. The reviewer reads everything twice — once as prose, once as code — and the prose goes stale the moment the code moves.

## The rules

- **Match the code around you.** Read the file you are changing and comment at that density. A file with few comments gets few new ones. That density is a ceiling, never a target. You never add a comment to reach it, and a file that already has many comments is not a reason to add more.
- **Drop the comment when the code already says it.** A clear name, a type, or a short function says the same thing and never goes out of date.
- **Keep the comment that says what the code cannot** — why this choice and not the obvious one, a gotcha (a bug worked around, an order that must hold), or an invariant a caller has to respect.
- **Cut narration.** Restating the next line, labelling sections, or walking through the steps: remove those.
- **Keep what the repo requires**: doc comments on public APIs, licence headers, linter directives, framework annotations. Those are structure, not narration.
- **Cutting a comment is not cutting information.** If the point matters and the code cannot carry it, rename something, split the function, or put it in the PR description.

## The test

Read your diff with the comments covered. Where you cannot follow the code, ask what helps more: a better name, or the comment. Keep the comment only when the answer is the comment.

## One example

Before:

```ts
// Get the items for this account
const items = await listItems(accountId)
// Loop through the items and add up the amounts
let total = 0
for (const item of items) {
  // Refunds are stored as negative amounts, so they subtract here on purpose.
  total += item.amount
}
// Return the total
return total
```

After:

```ts
const items = await listItems(accountId)
let total = 0
for (const item of items) {
  // Refunds are stored as negative amounts, so they subtract here on purpose.
  total += item.amount
}
return total
```

Same code. Three comments that repeated it are gone, and the one that stayed says something the code cannot.
