# Study protocol: non-experts building hardware with Hands

A small, honest pilot study (3-5 participants). It is meant to produce descriptive evidence
and qualitative insight, not statistical claims.

## Research questions

1. Can people with no electronics experience get a working gadget from a one-sentence idea
   by following Hands?
2. How long does it take, and where do they get stuck?
3. How many wiring mistakes do the instruments catch, and how many of those are caught
   **before** power is applied?
4. Does anything unsafe happen? (Target: zero unsafe events.)

## Participants

- 3-5 adults (18+) who say they have never built a circuit. Exclude anyone who has soldered,
  used an Arduino or Raspberry Pi, or studied electrical engineering.
- Recruit by convenience: friends, coworkers, a club. Record this as a limitation.
- No payment is needed. If you do pay, pay everyone the same amount, including people who
  stop early.
- If you are at a university or company, check whether this needs ethics/IRB review before
  you recruit. A study of adult volunteers doing a low-risk task is often exempt, but that is
  their call, not yours.

## Materials

- One starter kit per session (`SHOPPING_LIST.md`). Before each session, run
  `hands bringup` on that kit to confirm the hardware works.
- A laptop with Hands installed, and `hands doctor --real` all green.
- The web wiring guide on a second screen or a phone (`hands guide <gadget> --web`).
- A timer, this protocol, the consent form below, and a notes sheet.
- **Safety kit:** the USB cable within the participant's reach. Mains power, batteries and
  extra parts stay out of the room.

## Tasks

Every participant does the same gadget first, so times can be compared:

1. **Warm-up (5 min, not timed):** the facilitator shows how a jumper wire goes into a hole,
   and where rows and columns are labelled.
2. **Main task:** `steelers` (desk light + score buzzer). Start the session with:
   ```bash
   hands run steelers --real --participant P01 --camera prompt
   ```
   Add `--vision-model ...` if one is set up. Otherwise the participant answers the yes/no
   check questions.
3. **Optional second task** if time allows: `tempdisplay` or `servo`.

Time limit: **60 minutes** per task. Stop at the limit and record the session as not finished.

## Facilitator rules

- Read this aloud: *"We're testing the software, not you. Think aloud if you can. You can
  stop at any time."*
- **Do not help with the task.** You may only (a) repeat what is on screen, (b) answer
  "I can't help with that, follow what Hands says", and (c) step in for safety.
- **Step in immediately** if anything gets hot, smells, or smokes, if a part is inserted
  while USB is plugged in, or if the participant is unsure and about to power on. Say
  "unplug the USB cable". Then log it as a *facilitator safety intervention* in your notes.
- Any other help counts as an assist. Note the time and the reason. Report assists
  separately: they are a key result.

## Measures

Collected automatically in `metrics/runs.jsonl` (one session per run, tagged with
`--participant`):

| Measure | Event(s) |
|---------|----------|
| Time to working device | `run_start` → `run_end` (`metrics.time_to_working_s`) |
| Wiring steps and pace | `wiring_step` timestamps |
| Mistakes caught before power | `photo_check`, `intervention` with phase `pre_power` |
| Mistakes caught after power | `smoke_test`, `intervention` with phase `post_power`, `diagnosis` |
| Interventions (fix instructions) | `intervention` |
| Safety events | `refused`, failed `smoke_test`, `power_cut`, `firmware_lint` |
| Model usage | `metrics.llm_calls` |

Collected by hand on the notes sheet:

- Facilitator assists and safety interventions, with time and reason.
- Where the participant hesitated or was confused (step number and a quote).
- After the task, three 1-7 ratings: *confidence* ("I could build another one with Hands"),
  *clarity* ("the instructions were clear"), and *safety* ("I felt safe").
- One open question: "What was the hardest moment?"

## Analysis

```bash
hands report --format md --out study-report.md --title "Hands pilot, <date>"
```

The report gives per-session and aggregate numbers: success rate, median/IQR time to a
working device, mistakes caught before vs. after power, interventions, and safety events.
Real sessions and simulated sessions are kept in separate sections. Add the hand-collected
measures as a table, and quote participants.

Report **every** session, including failures, withdrawals and every safety intervention.
With n = 3-5, describe the results; don't claim significance.

## Data handling

- Use participant ids (P01…), never names, in logs, notes and reports.
- Photos of the breadboard stay on the study laptop. Don't photograph faces or hands where
  that can be avoided. Delete photos after analysis unless the participant agreed otherwise.
- Store signed consent forms apart from the data.
- A participant can ask for their data to be deleted at any time before publication.

---

## Consent form (template)

**Study:** Building a small electronic gadget with an AI assistant ("Hands")
**Researcher:** ______________________ **Contact:** ______________________

**What you will do.** You will follow on-screen instructions to put small electronic parts
on a breadboard (a plastic board with holes), connected to a laptop by USB. The session
takes up to about 75 minutes, including a short questionnaire.

**Risks.** Everything runs from the laptop's USB port: 5 volts, the same as charging a phone.
There is no mains (wall) electricity and there are no batteries. Small parts can have sharp
legs. A wrongly wired part can get warm. If that happens you will be asked to unplug the USB
cable, which removes all power at once. Small parts are a choking hazard, so keep them away
from children. The researcher will watch the whole session and can stop it at any time.

**What is recorded.** Timestamps and events from the software (which steps you did, which
mistakes were caught), photos of the breadboard (not of you), the researcher's notes, and
your questionnaire answers. Your name is not stored with any of this. You get a participant
number instead.

**How it is used.** In a write-up about how well the software helps beginners. Results are
reported in groups, or with participant numbers only. Short anonymous quotes may be used.

**Voluntary.** You can stop at any time, for any reason, without giving a reason. You can
ask for your data to be deleted up until ______________________ (date).

**Questions or concerns:** ______________________

By signing, you confirm you are 18 or older, you have read this form, your questions have
been answered, and you agree to take part.

Name: ______________________ Signature: ______________________ Date: __________

☐ I agree that anonymous photos of my breadboard may be shown in the write-up.
☐ I agree to be contacted about follow-up studies.

Researcher signature: ______________________ Date: __________
