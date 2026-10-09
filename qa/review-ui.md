# Installer wizard: UI/UX QA review

Scope: ui/src (App.tsx, api.ts, styles.css, Logo*.tsx) and installer/app.py. I checked the running build at
http://127.0.0.1:8088 at 1024px and 375px widths. I did not click Deploy, Upgrade or Destroy.
"Verified" means I reproduced it in the browser. The other findings come from reading the code.

## Critical

1. **A page reload during a run, then the rail, wipes the saved answers.** App.tsx:38 jumps to `deploy` with `answers = {}`
   when a run is active, and nothing loads the answers. If the run fails, "Back to review" (App.tsx:599) shows Review with
   `kube context undefined` everywhere. Any rail click there calls `go()`, which runs `PUT /api/answers {}` (App.tsx:55).
   The server replaces the whole answers object (app.py:127-142), so target, AI mode, admins and owner are lost.
   Only the secrets survive.
   Fix: add `api.getAnswers()` (GET /api/answers already exists) and call it in the status effect when `s.unlocked`.
   Also never PUT while `answers` is empty.
2. **"Start over" overwrites the saved vault with no confirmation** (App.tsx:215, app.py:106). The vault holds the
   generated Jenkins and Argo CD passwords and the OAuth App. Losing them breaks any later Upgrade or Destroy of a
   live install. Fix: ask for confirmation first (for example "Type start over"), or keep `generated` and `oauth`.
3. **Review can get stuck on "Looking at the cluster" forever (verified).** With no target, `detect_existing` reads
   `a["target"]["kind"]` outside its try block (plans.py:675) and returns a 500. Review's catch (App.tsx:495) sets only
   `err`, so `found` stays null and the button stays disabled. The user sees "Request failed (500)" and nothing else.
   Fix: in the catch, call `setFound({installed:false})`. Also use `a.get("target", {}).get("kind")` in plans.py.
4. **Review and Deploy are reachable with every field empty (verified).** Continue is never gated (App.tsx:122).
   Review showed `undefined@undefined over SSH` and `Model server at undefined`, with Deploy enabled.
   Fix: return `missing` from a `GET /api/check/answers` call that uses `validate()`, or mirror it per step.
   Disable Continue for steps with missing required fields, and list the gaps on Review with links back to each step.
   Render "Not set" instead of `undefined` (App.tsx:498-502).
5. **validate() does not check `repos.owner`** (app.py:261). An empty owner passes validation. Add `if not _get(a,"repos.owner")`.

## High

6. **The GitHub login is dropped, so the "Add <login>" shortcut never appears.** `/api/check/github` stores `repos.login`
   on the server (app.py:170), but the client never patches it into `answers` (App.tsx:342). The next Continue
   PUTs answers without it and erases it. Fix: after the check, run `patch(x => { x.repos = {...x.repos, login: res.login} })`.
7. **Deploy with no run shows an empty "Deploying" page.** The EventSource gets a 404 (app.py:286-287) and there is no
   `onerror`, so `run` stays null (App.tsx:560). Also, after a dropped connection the EventSource reconnects with
   `?start=0` and appends every line again, which duplicates keys and log lines.
   Fix: add `es.onerror` that shows "No deploy is running" with a Back button. Track `lines.length` and reconnect
   with `start=n`, or reset `lines` on `open`.
8. **Action card errors are hidden and its fields keep stale values.** `continueRun(...).catch(() => undefined)`
   (App.tsx:624) swallows a 409 or other failure. `<ActionCard>` has no `key`, so the next action reuses the
   previous `values` (App.tsx:593). Fix: use `key={run.action.step + run.action.message}`, show the error in an
   `alert`, and keep the button disabled until the state changes.
9. **Number fields snap back to their default while you type (verified).** Clearing SSH port and typing 2222
   produced `222222`, because `Number(v) || 22` refills 22 at once. The same happens for Nodes and the tailnet Port
   (App.tsx:276, 325, 422). There is no range check, and 222222 was saved. Fix: store the raw string and parse it on
   save. Validate the port (1-65535) and node count (1-10).
10. **Raw Python errors reach the user (verified):** "Could not look at the cluster yet: 'private_key'".
    `detect_existing` (plans.py:683) returns `str(KeyError)`. Fix: validate before `detect_existing`. If answers are
    missing, return "Fill in <field> first" and skip the cluster check.
11. **The focus ring is invisible in light mode.** The marker #d6ee4a on paper #eef2ec is 1.15:1 (styles.css:62).
    The input focus ring (brand-soft) is 1.21:1 (styles.css:137). Fix: in light mode use `outline: 3px solid var(--brand)`
    with a 2px offset, and keep the marker outline in dark mode (13:1).

## Medium

12. **On mobile, the rail buttons have no accessible name and tiny targets (verified).** `.rail-label{display:none}`
    (styles.css:242) leaves each button with no text, and each is 36x17px. Fix: hide the label visually with an
    sr-only class instead of `display:none`. Make the targets at least 24px tall. Add a visible "Step 3 of 7:
    Repositories" line above the h1 at widths under 820px.
13. **Focus is not moved when the step changes (verified).** `activeElement` was BODY after Continue. Screen reader
    users get no announcement. Fix: put `tabIndex={-1}` on each step's h1 and focus it in an effect on `step`.
14. **The rail cannot go forward to steps you already visited (verified).** `i <= index` (App.tsx:87) means that after
    going back to "Where" you must click Continue through every step. Fix: track `maxVisited` and allow `i <= maxVisited`.
15. **Review has no Back button**, and on mobile there are no labels to show where the rail goes. Show the `.nav`
    Back button on Review too (App.tsx:116).
16. **The log is announced line by line.** `<pre aria-live="polite">` (App.tsx:607) streams hundreds of lines to
    screen readers. Fix: use `role="log" aria-live="off"`, and add a separate sr-only live region that announces
    only step changes, "Your turn" and failures.
17. **Progress status is shown by colour only** (App.tsx:585-589). Add sr-only text ("done", "running", "waiting
    for you", "failed") to each item, and let `aria-current="step"` mark the current one.
18. **The light-mode `--faint` color (#7f8c85) fails contrast:** 3.1:1 on paper and 3.5:1 on white. It is used for
    every `.hint`, the todo rail labels and the progress titles (styles.css:15, 100, 138, 205). Darken it to about
    #66736c (4.5:1 or better). `--warn` (#a8660e) is 4.06:1 on paper, so darken it to about #8f560a.
19. **The dark-mode Destroy button fails contrast:** white on #ef7d6e is 2.68:1 (styles.css:163). Use
    `color: var(--brand-ink)` or dark text in dark mode.
20. **Input borders are 1.7:1** against their background (--line-strong on surface, styles.css:134), below the 3:1
    needed for non-text UI. Darken `--line-strong` for input borders.
21. **The "Test from the installer" button has no busy state or error handling** (App.tsx:409-410). A rejection is
    unhandled, and the result stays on screen after the endpoint changes. Fix: add busy/try-catch, and clear `probe`
    in the endpoint `onChange`.
22. **The reduced-motion rule misses part of the motion.** It covers the rail mark and logo, but the `transition`
    on `.rail-bar::before` and the buttons still runs, and `scrollTo` is fine. The `breathe` loop is killed only by
    `!important`. That works, but scope it to `.p-running .rail-mark` too, for clarity (styles.css:248-250).

## Low

23. **Copy buttons all have the same accessible name "Copy"** (App.tsx:176). Use `aria-label={"Copy " + label}`, and
    announce "Copied" through `role="status"`. There is also no `.catch` when the clipboard is denied.
24. **The `radiogroup` has no name** (App.tsx:161). Add `aria-labelledby` pointing at the step h1.
25. **Placeholders look like real values:** "elad", "10.0.0.39", "100.93.244.39" (App.tsx:262-264, 421). Use neutral
    examples such as "ubuntu" and "192.168.1.20", or drop them.
26. **The passphrase field is not autofocused on Unlock**, and "At least 8 characters" appears only for a new vault
    (App.tsx:209-211). Add `autoFocus` and show the rule in both cases.
27. **`.log` uses fixed colors** (#111a17 and others, styles.css:227-232) rather than tokens. That is fine in both
    themes, but move them to tokens for consistency.
28. **Done hides the logins and the Argo CD password.** `result.logins` is unused, and the hint tells users to run a
    long base64 `kubectl` command (App.tsx:667-668). Show "admin" plus a reveal-and-copy for both passwords, or drop the hint.
29. **"Open GitHub" is hard-coded for any `https://` command** (App.tsx:630). Use "Open link", or derive the hostname.
    A command that starts with `#` (plans.py:223) gets a useless Copy button.
30. **After a reload, a finished run's Done page cannot be reached** (App.tsx:38 handles only running and waiting).
    Offer "Show last result" on Unlock when `status.run.status === "done"`.

## Copy (plain, short, no teaching)

31. **Action names for removal are inconsistent:** "Remove the install", "Destroy", "Removing the install" and
    "Removed" (App.tsx:541-545, 576-582). Pick "Remove" everywhere, or "Destroy" everywhere. Likewise the button
    says "Upgrade the install" while the title says "Upgrading". Use "Upgrade".
32. **Some copy teaches instead of asking.** Cut the Repos lead "Jenkins builds from the three code repositories..."
    (App.tsx:348). Cut the Review rows "Images" and "Deploys" (App.tsx:506-507). Shorten the Tailscale paragraph
    (App.tsx:426-429) to a single line with a link. Cut "Every push to main now goes through Jenkins..." on Done (App.tsx:659).
33. **Wording fixes:**
    - "Answers saved, encrypted" shows before anything is saved on a fresh vault. Show it only after the first save.
    - "Test from the installer" should be "Test connection".
    - "Check access" is fine.
    - On Unlock, "Start" should be "Continue" to match the other steps.
34. **Error text from `/api/run`** reads "Missing: which cluster (kube context); GitHub token". It is clear, but it
    appears only after clicking Deploy, below the summary. Show it as a list, with each item linking to its step
    (ties to #4).
