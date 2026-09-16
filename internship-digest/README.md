# Your daily mechanical internship digest

This folder holds a small program that checks the jobright engineering-internship
list once a day, keeps only the postings that are relevant to a mechanical
engineering student, removes the ones you could not legally take on CPT or OPT,
and emails you what is left.

You do not need to know how to code to use it. Everything you would normally
want to change lives in one file, **`config.yaml`**, and it is written in plain
English.

---

## 1. What you will get

**One email a day.** Either a list of new jobs, or a one-line note saying
nothing new came up. If the program itself breaks, it emails you about that too,
so silence never means "no jobs" when it really means "it stopped working".

Each job in the email shows a coloured visa label:

| Label | Meaning | What to do |
|---|---|---|
| **A - Likely OK** | The job description was read and nothing rules out international students, or it actively mentions sponsorship | Apply |
| **B - Unclear** | No visa information could be found either way | Apply, but ask about sponsorship early |
| **C - Excluded** | Citizenship, clearance, ITAR or export-control language, or a defence employer, or outside the US | Removed for you - never shown |

> Treat **A** as "nothing obviously blocks you", not as a promise. Always confirm
> sponsorship with the employer before you invest time in an application.

You also get a spreadsheet, `data/jobs.csv`, listing every mechanical job the
tool has ever found, with an empty **application_status** column for your own
notes. The program never overwrites that column, so you can track applications
in it safely.

---

## 2. One thing you should know first

The source list is **not** a 7-day archive, even though the page says so. It only
ever holds the **most recent 100 postings**, and it is rewritten roughly every
hour. When this was measured, **3,098 different jobs** appeared over about two
and a half days, and in one hour the entire list of 100 was replaced.

Checking once a day therefore sees roughly 100 of the ~1,200 postings made that
day. You chose once a day, and that is what is set up. Just know that you are
seeing a sample, not everything.

When a run finds that *every* posting is new, the email says so, because that
means jobs came and went while you were not looking.

**If you want to catch more,** open `.github/workflows/internship-digest.yml`
and add one line under `schedule:`:

```yaml
    - cron: "0 * * * *"     # also check every hour
```

You will still receive only one email per day; the extra runs just collect jobs.
(See *Checking more often*, below.)

---

## 3. Setting it up on GitHub (about 15 minutes, free)

### Step A - Turn on Actions

"Actions" is GitHub's free robot that runs programs on a schedule.

1. Go to your repository on github.com.
2. Click the **Actions** tab.
3. If you see a green button saying *"I understand my workflows, go ahead and
   enable them"*, click it.

> **This step is required.** Your repository is a fork of the jobright repo, and
> GitHub switches scheduled jobs **off by default in forks**. If you skip this,
> nothing will ever run and you will not be told why.

### Step B - Get a free email key

Your Outlook address can receive the digest, but Microsoft blocks most scripts
from *sending* through it. We use Resend instead, which is free for 3,000 emails
a month.

1. Go to **https://resend.com** and sign up.
   **Sign up with `dontae.blake@outlook.com`** - on the free plan without your
   own web domain, Resend only lets you send mail to the address you registered
   with. Using a different address here is the single most common reason this
   step fails.
2. In the Resend dashboard click **API Keys** -> **Create API Key**.
3. Give it any name, choose **Sending access**, and click create.
4. Copy the key. It starts with `re_`. You only get to see it once.

### Step C - Store the key in GitHub

Never paste the key into a file - anything in the repository is visible to
anyone who can see the repository.

1. In your repository, go to **Settings** -> **Secrets and variables** ->
   **Actions**.
2. Click **New repository secret**.
3. Name it exactly `RESEND_API_KEY` (capital letters and underscores, no spaces).
4. Paste the key into the value box and click **Add secret**.

### Step D - Put the workflow on your default branch

GitHub only runs scheduled jobs from a repository's **default branch**, which
here is `master`. While this code sits on another branch you can run it by hand,
but the daily schedule will not start until it is merged into `master`.

### Step E - Test it right now

1. Go to the **Actions** tab.
2. Click **Internship digest** in the left-hand list.
3. Click **Run workflow**. Leave "Preview only" unticked to receive a real email.
4. Wait about a minute, then check your inbox - **including the junk folder**,
   since this will be the first message from this sender.

If the email arrives, you are finished. It will now run by itself every morning
at 7am US Eastern (6am in winter).

---

## 4. Changing what you see

Open **`config.yaml`** and edit it on github.com (click the file, then the pencil
icon, then *Commit changes*). You never need to touch any other file.

**Getting jobs you do not want?**
Add a word to the `drop:` list under `titles:`. Copy an existing line, keep the
two spaces and the dash:

```yaml
    - petroleum
```

**Missing jobs you do want?**
Add the word to `keep_strong:` instead.

**Want a different part of the country?**
Edit `preferred_states:` - these only affect ranking, not what is included.

**Too many emails with too much in them?**
Change `max_in_email:` from 25 to a smaller number. Everything still lands in
the spreadsheet.

**Want to see the excluded jobs after all?**
Set `include_unclear: false` to hide uncertain ones, or remove a company from
`exclude_companies:` if you think it is wrong.

A word about the defence-employer list: firms like Northrop Grumman and RTX are
excluded outright because their engineering work is export-controlled by US law
and effectively closed to F-1 students, even when the posting does not say so.
Companies such as Textron, which do both defence and civilian work, are *not*
excluded - those show up as **B - Unclear** for you to judge.

---

## 5. Checking more often

Once a day means you see about one posting in twelve. To collect more, add an
hourly line to the schedule as shown in section 2. GitHub's free tier allows
2,000 minutes a month on private repositories and unlimited minutes on public
ones; a run takes about a minute, so 24 runs a day fits comfortably in either.

If you later want collection and emailing split apart - collect hourly, email
once - ask and it can be wired up; the code already keeps those two jobs
separate internally.

---

## 6. Checking that it is working

- **Actions tab** - every run is listed with a green tick or a red cross. Click
  any run to read what it did.
- **`data/errors.log`** - warnings and failures, kept in the repository so they
  survive after GitHub deletes old logs.
- **`data/jobs.csv`** - should grow over time.
- **No email at all for two days?** The schedule is probably not running. Check
  Step A and Step D above.

GitHub pauses scheduled jobs in repositories that see no activity for 60 days.
This one commits its results daily, which counts as activity, so it will not
pause while it is working.

---

## 7. Running it on your own computer instead

Not required, but useful for testing. You need Python 3.9 or newer.

```bash
cd internship-digest
pip install -r requirements.txt

python3 digest.py --dry-run     # show what would be sent; changes nothing
python3 test_digest.py          # run the self-checks; should print OK
```

To send a real email you need the key in your terminal session first:

```bash
export RESEND_API_KEY="re_your_key_here"
python3 digest.py
```

---

## 8. What each file does

| File | Purpose |
|---|---|
| `config.yaml` | **The only file you need to edit.** All the word lists and settings |
| `digest.py` | Runs the whole job, start to finish |
| `jobparser.py` | Reads the table, fixes the `↳` arrows, tidies up locations and dates |
| `filters.py` | Decides mechanical vs not, and works out the visa bucket |
| `enrich.py` | Opens job pages to read their descriptions |
| `mailer.py` | Builds and sends the email |
| `store.py` | Looks after `seen.json` and your spreadsheet |
| `test_digest.py` | 44 self-checks proving the tricky parts behave |
| `data/seen.json` | Every job already reported, so you are not told twice |
| `data/jobs.csv` | Your running spreadsheet |
| `data/errors.log` | Problems worth knowing about |

---

## 9. Known limits

- **Job descriptions may not be readable.** It could not be verified from the
  build environment whether jobright.ai shows descriptions to visitors who are
  not logged in. The tool tries, and carries on without them if not - jobs simply
  stay in bucket **B**. The email tells you which mode the run was in. If they
  turn out to be unreadable, say so and the tool can be pointed at employer
  career pages instead.
- **Visa classification reads words, it does not reason.** A posting that
  mentions sponsorship only in a PDF, or an interview, will be misfiled. Bucket
  **A** means "nothing found that excludes you", never a guarantee.
- **Title filtering is keyword-based.** An unusually-worded mechanical job can
  be missed. If you spot one, add a word to `config.yaml`.
- **Only United States postings are kept**, because CPT and OPT do not authorise
  work abroad. Change `us_only:` if you ever need that.
- **The fit score is provisional.** It currently ranks on title wording, visa
  bucket, work model and location. Once your resume is added it can also match
  your actual coursework, software and project experience.
