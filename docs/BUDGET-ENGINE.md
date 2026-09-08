# Budget engine

## The rule that matters most

**Never average over months you do not have.** A gap in statement coverage silently drags
an average toward zero. Every statistic is computed over *covered* months only, and
`sample_months` travels with every suggestion so the interface can be honest about how much
it actually knows.

## Inputs

Monthly totals per category, drawn from `history_txns` and `transactions`, restricted to
months present in `month_coverage`.

## Method

Over a rolling window of 12 covered months:

| Step | Method |
|---|---|
| Totals | Sum per covered month |
| Trim | Drop the top and bottom 10% of months |
| Centre | Trimmed mean; median reported alongside |
| Spread | p25, p75 and median absolute deviation |
| Trend | First half vs second half of the window, as a percentage |

Trimming exists so that one car repair does not permanently inflate an auto envelope. The
trimmed months are not discarded from view — they are listed, so an excluded cost is
visible rather than silently dropped.

## Classification

| Class | Test | Suggestion |
|---|---|---|
| `fixed` | Same payee, regular cadence, low variance | The typical amount |
| `variable` | Regular, varying amount | Trimmed mean, with p75 offered as "comfortable" |
| `sinking` | 1–3 occurrences a year, large | Annual total ÷ 12, labelled "set aside monthly" |
| `oneoff` | Isolated, large, no cadence | Excluded from the baseline, listed |
| `insufficient` | Fewer than 3 covered months | No figure at all |

`sinking` earns its own class because annual costs are where budgets quietly fail. Averaged
into a 12-month mean they vanish into noise; ignored, they arrive as a surprise. Naming the
class is what lets the interface say *set aside £100 a month for this*.

## What the model does

**It never produces, adjusts or sees a number.**

That constraint is the whole design. Models are unreliable at arithmetic over hundreds of
rows, and a figure you cannot reproduce is not a budget. Every number in this engine and
in the savings plan is recomputable from the same inputs by running the code again.

What it does instead is one judgement about language: given a category *name*, how much
freedom does someone have to spend less on it? "Rent" is contractual, "Going Out" is not.
That is a question about what words mean, which is what a model is actually good at, and
it is a question arithmetic cannot answer at all — `stats.py` can see that Rent costs
£519 a month but has no way to know you cannot simply decide to pay less.

The answer is one of three words per category, stored on the category. The savings plan
then does its own arithmetic from your own months. Change the word and the numbers change
deterministically; the model is not consulted again.

Your answer outranks the model's. A flexibility you set by hand is never overwritten,
because you know which of your bills are actually fixed and the model is guessing from a
label you wrote.

## Privacy

The budget engine sends nothing anywhere. Every figure is computed on the machine.

The savings plan's classification step sends **category names only** — the labels you
typed, such as "Eating Out" and "Rent". Not amounts, not dates, not payees, not balances,
not account numbers, and not the plan itself. The judgement does not depend on the
amounts, so they are not sent: "Rent" is contractual whether it is £200 or £2,000.

It is off unless you enable it in Settings, and the answer is cached on each category, so
it runs once per category ever. Pressing the button again with nothing new costs nothing.

Merchant categorisation is a separate feature with the same shape: normalised merchant
names only, off by default.

## Confidence

Displayed, never hidden. The chart draws a band from p25 to p75; its width *is* the
confidence. Twelve covered months with low variance is a narrow band. Three months with
wild spread is a wide one, and reads as the guess it is.
