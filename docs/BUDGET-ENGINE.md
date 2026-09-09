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
in the targets is recomputable from the same inputs by running the code again.

There is one judgement arithmetic cannot make: given a category, how much freedom do you
have to spend less on it? `stats.py` can see that Rent costs £638 a month and has no way
to know you cannot simply decide to pay less. That is what the *flexibility* on each
category records — cannot be cut, some room, room to cut — and you set it yourself from
the category's detail panel on the budget screen.

It decides one thing: whether the target is allowed to sit below the forecast. A fixed
cost is budgeted at what it will actually be; a flexible one is trimmed towards a month
you have already had. Change the word and the numbers change deterministically.

## Privacy

The budget engine sends nothing anywhere. Every figure is computed on the machine.

Merchant categorisation is a separate feature with the same shape, and off by default.
It sends normalised merchant names plus up to forty `merchant -> category` examples from
your own history, so the model can follow your distinctions instead of guessing at them —
a canteen at work and a takeaway are both restaurants, and only your own filings say
which is which. Still no amounts, dates or balances.

## Confidence

Displayed, never hidden. The chart draws a band from p25 to p75; its width *is* the
confidence. Twelve covered months with low variance is a narrow band. Three months with
wild spread is a wide one, and reads as the guess it is.
