# Claim Bot — How to Use

Submit and manage expense claims directly on WhatsApp.

---

## For Employees — Submitting a Claim

Message the company bot on WhatsApp (private chat) and follow the prompts.

**Steps**

1. Send **`claim`** to start.
2. First time only — the bot asks for your **name**. Reply with your full name (it remembers you after that).
3. Choose the **type**: reply **`1`** Taxi, **`2`** Food, or **`3`** Other.
4. Enter the **amount** (e.g. `12.50`).
5. Send a **photo of the receipt** 📷.
6. For **Other**, add a short **note** describing the expense.
7. Done ✅ — the bot confirms and shows your total for the month.

**Handy commands**

| Send | What it does |
|------|--------------|
| `claim` | Start a new claim |
| `mine` or `history` | See your claims this month |
| `cancel` | Cancel the claim you're entering |

**Tips**
- One claim at a time. After it confirms, send `claim` again for the next one.
- Make sure the receipt photo is clear and shows the amount.

---

## For Finance — Getting Reports

You receive reports on WhatsApp and can request them anytime. Your finance
number is pre-configured by the admin, so no setup is needed — just send a command.

**Commands**

| Send | What you get |
|------|--------------|
| `report <name>` | That employee's claims for **this month** (Excel) |
| `report <name> 2026-05` | That employee's claims for a specific month |
| `report all` | All employees for this month |
| `employees` | List of staff and their totals this month |

Examples: `report Monica` · `report John 2026-05` · `report all`

**Automatic monthly report**
On the 1st of each month, the bot automatically sends you every employee's report for the previous month.

**About the files**
- One **Excel file per employee**, named `Name_YYYY-MM.xlsx`.
- Each file has **3 tabs**: Taxi, Food, Other — with a subtotal on each.
- The **Receipt** column shows a **📎 View receipt** link. Open the Excel in a spreadsheet app (Excel / Google Sheets / Numbers), then tap the link to view the receipt photo in your browser.
- Approve by marking the **Status** column as you review.

> Receipt links expire after 60 days. If you need an older receipt, just request the report again (`report <name> 2026-03`) and a fresh link is generated.
