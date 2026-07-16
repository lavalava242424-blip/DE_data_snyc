"""
স্বয়ংক্রিয় নিউজ বট — RSS + DeepSeek Rewrite + 6h লুপ + দৈনিক টার্গেট (প্রতি রানের সীমা)

আপডেট: generate_article() এখন সামারি নেয় না — শুধু টাইটেল ও লিংক পাঠায়,
DeepSeek-এর Search টগল অন থাকায় ও নিজেই লিংকে গিয়ে বিস্তারিত পড়ে নেয়।
"""
import os, json, base64, pickle, random, time, requests, jinja2, feedparser, re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── কনফিগ ──
RSS_FEEDS = [
    "https://www.bd24live.com/bangla/feed/",
    "https://jagonews24.com/rss/rss.xml",
    "https://sarabangla.net/news/bangladesh/feed/",
]
FB_PAGE_ID = os.environ["FB_PAGE_ID"]
FB_PAGE_ACCESS_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
BLOGGER_BLOG_ID = os.environ["BLOGGER_BLOG_ID"]
FB_GRAPH_VERSION = os.environ.get("FB_GRAPH_API_VERSION", "v25.0")
GRAPH = f"https://graph.facebook.com/{FB_GRAPH_VERSION}"

STATE_FILE = "state.json"
MAX_RUN_SECONDS = 6 * 3600

# ── স্টেট ম্যানেজমেন্ট ──
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_checked": (datetime.utcnow() - timedelta(hours=2)).isoformat(),
        "posted_titles": [],
        "canonical_urls": [],
        "post_timestamps": [],
        "target_date": None,
        "daily_target": 0,
        "daily_count": 0,
    }

def save_state(state):
    for k in ["posted_titles", "canonical_urls", "post_timestamps"]:
        state[k] = state[k][-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def get_daily_target(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("target_date") != today:
        state["target_date"] = today
        state["daily_target"] = random.randint(50, 55)
        state["daily_count"] = 0
        save_state(state)
        print(f"🎯 New daily target: {state['daily_target']}")
    return state["daily_target"], state["daily_count"]

def increment_daily_count(state):
    state["daily_count"] = state.get("daily_count", 0) + 1
    save_state(state)
    c = state["daily_count"]
    print(f"📊 {c}/{state['daily_target']}")
    return c >= state["daily_target"]

def adaptive_delay(run_start, posts_left):
    elapsed = time.time() - run_start
    remaining = max(0, MAX_RUN_SECONDS - elapsed)
    if posts_left <= 0:
        return 60
    avg = remaining / posts_left
    delay = avg * random.uniform(0.7, 1.3)
    return max(30, min(int(delay), 1800))

# ── RSS / ডুপ্লিকেট ──
PREV_TITLES = []
PREV_URLS = []

def set_previous_data(titles, urls):
    global PREV_TITLES, PREV_URLS
    PREV_TITLES = titles[-500:]
    PREV_URLS = urls[-500:]

def add_to_previous(title, url):
    global PREV_TITLES, PREV_URLS
    PREV_TITLES.append(title)
    PREV_URLS.append(url)
    PREV_TITLES = PREV_TITLES[-500:]
    PREV_URLS = PREV_URLS[-500:]

def is_duplicate(title, url, th=0.85):
    if url and url in PREV_URLS:
        return True
    for t in PREV_TITLES:
        if SequenceMatcher(None, title.lower(), t.lower()).ratio() >= th:
            return True
    return False

def clean_title(title):
    if not title: return title
    for sep in [' | ', ' - ', ' – ', '।']:
        if sep in title:
            title = title.split(sep)[0].strip()
    return title

def extract_img(desc):
    if not desc: return None
    soup = BeautifulSoup(desc, "html.parser")
    img = soup.find("img")
    if img and img.get("src"): return img["src"]
    m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', desc)
    return m.group(1) if m else None

def get_image_url(entry):
    if "media_content" in entry:
        for m in entry.media_content:
            if m.get("url"): return m["url"]
    if "enclosure" in entry:
        for e in entry.enclosures:
            if e.get("url"): return e["url"]
    if "description" in entry:
        u = extract_img(entry.description)
        if u: return u
    if "content" in entry:
        for c in entry.content:
            u = extract_img(c.value)
            if u: return u
    return None

def fetch_latest_from_feeds(exclude_links=None):
    exclude_links = exclude_links or set()
    now = datetime.now(timezone.utc)
    feeds = RSS_FEEDS.copy()
    random.shuffle(feeds)
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries: continue
            entries = list(feed.entries)
            random.shuffle(entries)
            for entry in entries[:15]:
                title = clean_title(entry.title)
                link = entry.link
                if not title or len(title) < 10: continue
                if link in exclude_links: continue
                img = get_image_url(entry)
                if not img: continue
                if is_duplicate(title, link): continue
                return [{
                    "title": title, "link": link,
                    "image_url": img, "canonical_url": link,
                    "published": now.isoformat(),
                }]
        except Exception as e:
            print(f"Feed error: {e}")
    return []

# ── আপডেটেড কার্ড টেমপ্লেট ──
CARD_TEMPLATE_HTML = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<title>DemocraticEcho</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+Bengali:wght@400;600;700&family=Playfair+Display:ital,wght@0,600;1,600&display=swap" rel="stylesheet">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { width: 1080px; height: 1080px; background: #000; display: flex; align-items: center; justify-content: center; }
.card { width: 1080px; height: 1080px; position: relative; overflow: hidden; }
.card-image { width: 100%; height: 100%; object-fit: cover; object-position: center top; display: block; }
.overlay {
  position: absolute; inset: 0;
  background: linear-gradient(
    to top,
    rgba(0,0,0,0.92) 0%,
    rgba(0,0,0,0.75) 25%,
    rgba(0,0,0,0.35) 55%,
    rgba(0,0,0,0) 80%
  );
}
.content { position: absolute; bottom: 0; left: 0; right: 0; padding: 70px 64px; color: #fff; }
.category {
  display: inline-block;
  font-family: 'Playfair Display', serif;
  font-style: normal;
  font-size: 28px;
  font-weight: 700;
  letter-spacing: 1.5px;
  color: #000;
  background: #ffffff;
  padding: 13px 32px;
  border-radius: 4px;
  margin-bottom: 32px;
}
.headline {
  font-family: 'Noto Serif Bengali', serif;
  font-size: 54px;
  font-weight: 700;
  line-height: 1.4;
  color: #ffffff;
  margin-bottom: 24px;
}
.meta { display: flex; justify-content: flex-end; align-items: center; }
.date {
  font-family: 'Playfair Display', serif;
  font-style: normal;
  font-size: 16px;
  font-weight: 600;
  color: #ffffff;
  letter-spacing: 0.5px;
}
</style>
</head>
<body>
<div class="card">
  <img class="card-image" src="{{ image_data_uri }}" alt="news">
  <div class="overlay"></div>
  <div class="content">
    <div class="category">{{ category }}</div>
    <div class="headline">{{ title }}</div>
    <div class="meta">
      <div class="date">{{ date }}</div>
    </div>
  </div>
</div>
</body>
</html>"""

def image_to_base64(path):
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    ext = os.path.splitext(path)[1].lower()
    mime = "image/jpeg" if ext in [".jpg",".jpeg"] else "image/png"
    return f"data:{mime};base64,{data}"

def generate_card_html(title, img_path, category, date_str, out="temp_card.html"):
    tpl = jinja2.Template(CARD_TEMPLATE_HTML)
    html = tpl.render(title=title, image_data_uri=image_to_base64(img_path),
                      category=category, date=date_str)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out

def render_html_to_image(html_path, out_img):
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        page = browser.new_page(viewport={"width":1080,"height":1080})
        page.goto(f"file://{os.path.abspath(html_path)}")
        page.wait_for_load_state("networkidle")
        page.screenshot(path=out_img, clip={"x":0,"y":0,"width":1080,"height":1080})
        browser.close()

def create_news_card(title, img_path, out_path, category="সর্বশেষ", date_str="২৩ মে ২০২৬"):
    html_path = "temp_card.html"
    generate_card_html(title, img_path, category, date_str, html_path)
    render_html_to_image(html_path, out_path)
    return out_path

# ── DeepSeek Rewrite ──
def load_deepseek_session():
    s = os.environ.get("DEEPSEEK_SESSION_JSON")
    if s:
        try: return json.loads(s)
        except: pass
    if os.path.exists("deepseek_session.json"):
        with open("deepseek_session.json") as f:
            return json.load(f)
    return None

def _deepseek_ensure_toggle_on(page, label_text: str) -> None:
    try:
        toggles = page.query_selector_all("div[aria-pressed]")
        for t in toggles:
            span = t.query_selector("span")
            if span and span.inner_text().strip() == label_text:
                pressed = t.get_attribute("aria-pressed")
                cls = t.get_attribute("class") or ""
                is_on = (pressed == "true") and ("ds-toggle-button--selected" in cls)
                if not is_on:
                    t.click()
                    page.wait_for_timeout(random.uniform(400, 700))
                return
    except Exception as e:
        print(f"  ⚠️  DeepSeek toggle '{label_text}' error: {e}")

def _deepseek_is_focused(page, el) -> bool:
    try:
        return bool(page.evaluate(
            """(node) => {
                let p = node;
                for (let i = 0; i < 6 && p; i++) {
                    if (p.classList && p.classList.contains('focused')) return true;
                    p = p.parentElement;
                }
                return false;
            }""",
            el,
        ))
    except Exception:
        return False

def _deepseek_find_textarea(page, timeout=8000):
    for sel in [
        'textarea[name="search"]',
        'textarea[placeholder="Message DeepSeek"]',
        'textarea.ds-scroll-area',
        'textarea',
    ]:
        try:
            el = page.wait_for_selector(sel, timeout=timeout)
            if el and el.is_visible():
                return el
        except:
            continue
    return None

def deepseek_rewrite(browser, prompt):
    session = load_deepseek_session()
    if not session:
        print("  ❌ DeepSeek session not available — cannot rewrite.")
        return None
    ctx = browser.new_context(storage_state=session)
    page = ctx.new_page()
    try:
        print("  🌐 DeepSeek page loading...")
        page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        _deepseek_ensure_toggle_on(page, "Search")
        _deepseek_ensure_toggle_on(page, "DeepThink")

        textarea = _deepseek_find_textarea(page)
        if not textarea:
            print("  ❌ DeepSeek textarea not found.")
            return None

        textarea.click()
        page.wait_for_timeout(500)
        if not _deepseek_is_focused(page, textarea):
            textarea.click()
            page.wait_for_timeout(500)

        textarea.fill(prompt)
        page.wait_for_timeout(random.uniform(500, 800))

        sent = False
        try:
            btn = page.wait_for_selector(
                'div[role="button"].ds-button--primary.ds-button--circle', timeout=5000
            )
            if btn and btn.is_visible() and btn.is_enabled():
                btn.click()
                sent = True
        except:
            pass
        if not sent:
            page.keyboard.press("Enter")

        print("  ⏳ Waiting for DeepSeek response (Search + DeepThink, ~90s)...")
        page.wait_for_timeout(90000)

        response_text = ""
        last_text = ""
        stable_count = 0
        for _ in range(30):
            page.wait_for_timeout(2000)
            try:
                blocks = page.query_selector_all(
                    "div.ds-markdown.ds-assistant-message-main-content"
                )
                if blocks:
                    last_block = blocks[-1]
                    # Search ফিচারের সাইটেশন মার্কার (ভাসমান "１", "２"... নাম্বার)
                    # টেক্সট তোলার আগেই সরিয়ে ফেলা, নাহলে এগুলো আলাদা লাইনে
                    # স্ট্রে সংখ্যা হিসেবে ঢুকে যায়
                    last_block.evaluate(
                        "(el) => el.querySelectorAll('.ds-markdown-cite').forEach(n => n.remove())"
                    )
                    txt = last_block.inner_text().strip()
                    txt = re.sub(r'\n{3,}', '\n\n', txt).strip()
                    if txt:
                        if txt == last_text:
                            stable_count += 1
                        else:
                            stable_count = 0
                            last_text = txt
                        if stable_count >= 2:
                            response_text = txt
                            break
            except Exception:
                pass

        if not response_text and last_text:
            response_text = last_text

        REFUSAL_PHRASES = ["beyond my current scope", "i cannot", "i'm unable"]
        if response_text and any(p in response_text.lower() for p in REFUSAL_PHRASES):
            print(f"  🚫 DeepSeek refused: {response_text[:80]}...")
            return None

        if response_text:
            print(f"  ✅ DeepSeek response: {response_text[:100]}...")
            return response_text
        else:
            print("  ⚠️  DeepSeek no response.")
    except Exception as e:
        print(f"  ⚠️  DeepSeek error: {e}")
    finally:
        page.close()
        ctx.close()
    return None

def generate_article(headline, link):
    """সামারি না নিয়ে শুধু টাইটেল + লিংক পাঠানো হয় — DeepSeek-এর Search টগল
    অন থাকায় ও নিজেই লিংকে গিয়ে বিস্তারিত পড়ে নেয়।"""
    prompt = f"""{headline}
{link}

এ বিষয়ে একটি বিস্তারিত নিউজ আর্টিকেল লিখো
প্রফেশনাল নিরপেক্ষ লেটেস্ট 
শুধুমাত্র সাধারণ টেক্সট দাও, HTML নয়। কমপক্ষে ৩-৫ প্যারাগ্রাফ। কোনো সাবহেডিং প্রয়োজন নেই।
ভাষা হবে সংবাদমাধ্যমের মানসম্পন্ন ও নিরপেক্ষ বাংলা।"""
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=headless)
        try:
            text = deepseek_rewrite(browser, prompt)
        finally:
            browser.close()
    return text if text else headline

# ── Blogger & Facebook ──
def get_blogger_service():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    if creds:
        creds.refresh(Request())
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)
    else:
        raise Exception("token.pickle missing")
    return build("blogger", "v3", credentials=creds)

def post_to_blogger(title, html):
    service = get_blogger_service()
    post = service.posts().insert(
        blogId=BLOGGER_BLOG_ID,
        body={"kind":"blogger#post","title":title,"content":html},
        isDraft=False
    ).execute()
    return post["url"]

def post_image_and_comment(img_path, caption, comment):
    url = f"{GRAPH}/{FB_PAGE_ID}/photos"
    params = {"access_token":FB_PAGE_ACCESS_TOKEN,"published":"true","message":caption}
    with open(img_path,"rb") as f:
        r = requests.post(url, params=params, files={"source":f})
    data = r.json()
    if "error" in data:
        raise Exception(f"Facebook API error: {data['error']}")
    post_id = data.get("post_id") or data["id"]
    curl = f"{GRAPH}/{post_id}/comments"
    requests.post(curl, params={"access_token":FB_PAGE_ACCESS_TOKEN}, data={"message":comment})
    return post_id

# ── Helper ──
def bengali_date_today():
    months = {1:"জানুয়ারি",2:"ফেব্রুয়ারি",3:"মার্চ",4:"এপ্রিল",5:"মে",6:"জুন",
              7:"জুলাই",8:"আগস্ট",9:"সেপ্টেম্বর",10:"অক্টোবর",11:"নভেম্বর",12:"ডিসেম্বর"}
    bd = ["০","১","২","৩","৪","৫","৬","৭","৮","৯"]
    now = datetime.now()
    day = "".join(bd[int(d)] for d in str(now.day))
    year = "".join(bd[int(d)] for d in str(now.year))
    return f"{day} {months[now.month]} {year}"

def download_image(url, fname):
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, stream=True, timeout=15)
        if r.status_code == 200:
            with open(fname,"wb") as f:
                for chunk in r: f.write(chunk)
            return fname
    except: pass
    return None

# ── MAIN (প্রতি রানের সর্বোচ্চ পোস্ট সীমাসহ) ──
def main():
    print("🚀 Bot started")
    state = load_state()
    set_previous_data(state.get("posted_titles",[]), state.get("canonical_urls",[]))

    target, count = get_daily_target(state)
    if count >= target:
        print("🎯 Today's target already reached.")
        return

    max_run_posts = max(1, -(-target // 4))   # ceil division
    print(f"📌 This run will post up to {max_run_posts} articles (daily target: {target})")

    run_start = time.time()
    posts_done_this_run = 0
    failed_this_run = set()

    while True:
        if time.time() - run_start >= MAX_RUN_SECONDS - 30:
            print("⏰ 6h limit")
            break
        tgt, cnt = get_daily_target(state)
        if cnt >= tgt:
            print("🎯 Daily target reached")
            break
        if posts_done_this_run >= max_run_posts:
            print(f"🏁 Run limit reached ({max_run_posts} posts this run)")
            break

        articles = fetch_latest_from_feeds(exclude_links=failed_this_run)
        if not articles:
            time.sleep(300)
            continue

        art = articles[0]
        if art["title"] in state["posted_titles"] or art["link"] in failed_this_run:
            time.sleep(30)
            continue

        img_file = download_image(art["image_url"], "temp_news.jpg")
        if not img_file:
            failed_this_run.add(art["link"])
            print("⚠️ Image download failed, added to failed_this_run")
            time.sleep(30)
            continue

        card_path = create_news_card(art["title"], img_file, "card_output.jpg",
                                     "সর্বশেষ", bengali_date_today())
        print("🖼️ Card created")

        article_text = generate_article(art["title"], art["link"])
        paras = [p.strip() for p in article_text.split("\n") if p.strip()]
        html = "".join(f"<p>{p}</p>" if not (p.startswith("<p>") and p.endswith("</p>")) else p for p in paras)
        if art.get("image_url"):
            html = f'<img src="{art["image_url"]}" style="max-width:100%">' + html

        try:
            blog_url = post_to_blogger(art["title"], html)
            print(f"🔗 Blogger: {blog_url}")
            state["posted_titles"].append(art["title"])
            state["canonical_urls"].append(art.get("canonical_url", art["link"]))
            state["post_timestamps"].append(datetime.now(timezone.utc).isoformat())
            state["last_checked"] = art.get("published", datetime.now(timezone.utc).isoformat())
            add_to_previous(art["title"], art.get("canonical_url", art["link"]))
            save_state(state)
        except Exception as e:
            err_msg = str(e)
            print(f"❌ Blogger error: {err_msg}")
            if any(word in err_msg.lower() for word in ["invalid", "unauthorized", "token"]):
                print("🔐 Fatal auth error – stopping run.")
                break
            else:
                print("⚠️ Non-fatal Blogger error, skipping article.")
                failed_this_run.add(art["link"])
                time.sleep(30)
                continue

        try:
            post_image_and_comment(card_path, art["title"], f"বিস্তারিত: {blog_url}")
            print("📤 Facebook done")
        except Exception as e:
            print(f"❌ Facebook error: {e}")

        done = increment_daily_count(state)
        posts_done_this_run += 1

        if done:
            print("🎯 Daily target completed.")
            break
        if posts_done_this_run >= max_run_posts:
            print(f"🏁 Run limit reached ({max_run_posts} posts this run)")
            break

        posts_left_this_run = max_run_posts - posts_done_this_run
        delay = adaptive_delay(run_start, posts_left_this_run)
        print(f"⏳ Next in {delay//60}m {delay%60}s")
        time.sleep(delay)

    print("✅ Run finished")

if __name__ == "__main__":
    main()
