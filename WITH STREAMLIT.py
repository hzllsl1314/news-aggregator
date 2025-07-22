import requests
import time
import re
from datetime import datetime
from bs4 import BeautifulSoup
import json
import threading
import feedparser
import streamlit as st
import pandas as pd
from threading import Thread
import queue
import os
import io
import pdfminer.high_level as pdf
from uuid import uuid4


DATA_FILE = "rss_state.json" #variable to hold the filename where the script will save and load urls and keywords

def _load_state(): #load saved state from the file if exist
    if os.path.isfile(DATA_FILE): #if rs_state.json exists in the current directory
        try:
            with open(DATA_FILE, "r") as f: #open in read mode
                data = json.load(f) #read and parse the json into a python dictionary
                return data.get("urls", []), data.get("keywords", []) #extract the urls and keywords
        except json.JSONDecodeError:
            pass
    return [], []

def _save_state(urls, keywords): #to save the current urls and keywords into the json file
    with open(DATA_FILE, "w") as f: #open file for writing
        json.dump({"urls": urls, "keywords": keywords}, f, indent=2) #conver the provided urls and keywords lists into JSON format and write to file

def looks_like_json_api(url: str) -> bool:
    return url.lower().endswith(".json") or "/resource/" in url

def json_to_articles(data, url): #track useful information from json
    articles = []

    def visit(node): #function to recursively walk through the entire json
        if isinstance(node, list): #if node is a list, call visit () on it
            for x in node:
                visit(x)
        elif isinstance(node, dict): #if node is a dictionary
            # flatten every value that is a str/int/float/bool
            flat = " ".join(
                str(v) for v in node.values()
                if isinstance(v, (str, int, float, bool))
            ).strip() #create a flat string by joining all primitive values in the dictionary. The flat text serve as full text of an article

            # pick reasonable keys for title/link if they exist
            def pick(keys):
                for k in keys:
                    if k in node and isinstance(node[k], str):
                        return node[k]
                return None

            title = pick([
                "title", "name", "headline", "subject",
                "t_tol_de_la_norma",  # ca: Títol de la norma
                "t_tol_de_la_norma_es"  # es: Título de la norma
            ])
            link = pick([
                "url", "link", "pdf_url", "href",  # existing fall‑backs
                "format_html", "url_es_formato_html",  # keep – but these are dicts
                "url_ultima_versi_format_html"
            ])
            # NEW: if the value we just picked is still empty, look inside the nested dict
            if not link: 
                for k in ("format_html", "url_es_formato_html",
                          "url_ultima_versi_format_html"):
                    if k in node and isinstance(node[k], dict):
                        inner = node[k].get("url")
                        if isinstance(inner, str):
                            link = inner
                            break  
            date = pick([
                "published_at", "date", "created_at", "timestamp", "data_publicacio",
                "data_del_document",  # ca: Data del document
                "data_de_publicaci_del_diari"  # ca: Data de publicació del diari
            ])

            if flat:  # ignore empty dicts. Only add if flattened text exists
                articles.append({
                    "title": title or flat,
                    "url": link or url,
                    "description": "",
                    "source": url,
                    "full_text": flat,
                    "timestamp": date or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })

            # recurse into nested dicts
            for v in node.values():
                visit(v)

    visit(data)
    return articles


# Set page config
st.set_page_config(
    page_title="News Monitor",
    page_icon="📰",
    layout="wide"
)

class NewsMonitor: #create the class 
    def __init__(self):
        self.urls, self.keywords = _load_state()
        self.found_articles = set()  # To avoid duplicate alerts
        self.running = False
        self.articles_queue = queue.Queue() #queue list to order things in FIFO

    def add_url(self, url): #If url doesn't exist, add. If exists, returns false
        if url not in self.urls:
            self.urls.append(url)
            _save_state(self.urls, self.keywords)
            return True
        return False

    def remove_url(self, url): #If url exists (duplicated), remove.
        if url in self.urls:
            self.urls.remove(url)
            _save_state(self.urls, self.keywords)
            return True
        return False

    def add_keyword(self, keyword): #add keyword if not existing
        keyword_lower = keyword.lower()
        if keyword_lower not in self.keywords:
            self.keywords.append(keyword_lower)
            _save_state(self.urls, self.keywords)
            return True
        return False

    def remove_keyword(self, keyword): #remove keyword if duplicated
        keyword_lower = keyword.lower()
        if keyword_lower in self.keywords:
            self.keywords.remove(keyword_lower)
            _save_state(self.urls, self.keywords)
            return True
        return False

    def fetch_rss_feed(self, url):
        try:
            feed = feedparser.parse(url)
            if feed.bozo:
                print(f"RSS feed may have issues: {url}")
            return feed
        except Exception as e:
            print(f"Error fetching RSS feed {url}: {e}")
            return None

    def extract_articles(self, feed, url):
        articles = []

        if not feed or not hasattr(feed, 'entries'):
            return articles

        for entry in feed.entries:
            title = entry.get('title', 'No title')
            link = entry.get('link', url)
            description = entry.get('description', entry.get('summary', ''))

            full_text = f"{title} {description}"
            ts = entry.get("published") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            articles.append({
                'title': title,
                'url': link,
                'description': description,
                'source': url,
                'full_text': full_text,
                "timestamp": ts,
            })

        return articles

    def check_keywords(self, text):
        text_lower = text.lower()
        found_keywords = []

        for keyword in self.keywords:
            if keyword in text_lower:
                found_keywords.append(keyword)

        return found_keywords

    def scan_for_news(self):
        new_articles = []

        for url in self.urls: #loop through each url
            #1 is the url rss?
            feed = self.fetch_rss_feed(url)
            if feed and getattr(feed, "entries", []): #Try to fetch the RSS feed from the current URL
                for art in self.extract_articles(feed, url): #if we have entries/articles, continue
                    if self._article_matches(art): #go through each article and check if match criteria
                        new_articles.append(art)
                continue  # go to next URL if RSS worked

            #If no rss, try as josn api
            if looks_like_json_api(url):
                try:
                    resp = requests.get(url, timeout=10) #if api, treat as json
                    if "application/json" in resp.headers.get("content-type", ""):
                        data = resp.json()
                        for art in json_to_articles(data, url): #if json, store in python dict
                            if self._article_matches(art): #clean json into article objects
                                new_articles.append(art)
                except Exception as e:
                    print(f"Error reading JSON API {url}: {e}")

        return new_articles

    #check if the article contain any of the keywords. Check if it is new
    def _article_matches(self, article: dict) -> bool:

        found = self.check_keywords(article["full_text"])
        if not found:
            return False

        article_id = f"{article['url']}_{article['title']}"
        if article_id in self.found_articles:
            return False

        self.found_articles.add(article_id)
        article["keywords"] = found
        article["timestamp"] = article.get("timestamp") or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return True

    def monitoring_loop(self): #to monitor the news
        while self.running:
            try:
                new_articles = self.scan_for_news() #look for news
                if new_articles:
                    self.articles_queue.put(new_articles) #if new articles, put in queue
                time.sleep(600)  # 10 minutes
            except Exception as e:
                print(f"Error in monitoring loop: {e}")
                break

    def start_monitoring(self):
        if not self.running and self.urls and self.keywords: #if monitor not active, there are urls, and there are keywords
            self.running = True #set a flag so the rest of the code knows monitoring is running
            monitor_thread = Thread(target=self.monitoring_loop, daemon=True) #a new bg thread that will run the monitoring_loop method
            monitor_thread.start()#actual start
            return True #if monitoring started correclty, return true
        return False

    def stop_monitoring(self):
        self.running = False

    def get_new_articles(self):
        articles = []
        try:
            while True:
                articles.extend(self.articles_queue.get_nowait()) #gets the articles of the queue inmediately without waiting and adds to the articles list
        except queue.Empty: #if nothing int he queue, stop the loop
            pass
        return articles


# Initialize session state
if 'monitor' not in st.session_state:
    st.session_state.monitor = NewsMonitor()
if 'all_articles' not in st.session_state:
    st.session_state.all_articles = []
if 'monitoring_status' not in st.session_state:
    st.session_state.monitoring_status = False

monitor = st.session_state.monitor

# Main UI
st.title("📰 RSS News Monitor")
st.markdown("Monitor RSS feeds for news containing your keywords - Updates every 10 minutes!")

# Sidebar for configuration
st.sidebar.header("⚙️ Configuration")

# RSS Feed Management, let users add new RSS using the sidebar
st.sidebar.subheader("RSS Feeds") #section RSS Feeds
new_url = st.sidebar.text_input("Add RSS Feed URL:", placeholder="https://example.com/rss.xml") #text input box
if st.sidebar.button("Add RSS Feed"): #shows a button labeled as Add RSS Feed, and if user clicks, the if executes
    if new_url:
        if monitor.add_url(new_url): #if it is a new RSS, add to the object monitor
            st.sidebar.success(f"Added: {new_url}") #if successfully added, show a green message with added
            st.rerun()  #rerun the app to refresh and show the new feed in the UI
        else:
            st.sidebar.warning("RSS feed already exists!")
    else:
        st.sidebar.error("Please enter a URL")

# Display current RSS feeds added so far in the sidebar, You can remove by clicking a X button next to each one
if monitor.urls:
    st.sidebar.write("**Current RSS Feeds:**")
    for i, url in enumerate(monitor.urls):
        col1, col2 = st.sidebar.columns([3, 1])# 2 columns in the sidebar
        with col1:
            st.sidebar.write(f"{i + 1}. {url}")#display feed number and the url
        with col2:
            if st.sidebar.button("❌", key=f"remove_url_{i}"):#display the button to remove feed
                monitor.remove_url(url) #if button was clicked, remove the feed
                st.rerun()

# Keyword Management, let the users to add ne keywords
st.sidebar.subheader("Keywords")
new_keyword = st.sidebar.text_input("Add Keyword:", placeholder="technology") #text input box for users to type a keyword
if st.sidebar.button("Add Keyword"):
    if new_keyword:
        if monitor.add_keyword(new_keyword):
            st.sidebar.success(f"Added: {new_keyword}")
            st.rerun()
        else:
            st.sidebar.warning("Keyword already exists!")
    else:
        st.sidebar.error("Please enter a keyword")

# Display current keywords
if monitor.keywords:
    st.sidebar.write("**Current Keywords:**")
    for i, keyword in enumerate(monitor.keywords):
        col1, col2 = st.sidebar.columns([3, 1])
        with col1:
            st.sidebar.write(f"{i + 1}. {keyword}")
        with col2:
            if st.sidebar.button("❌", key=f"remove_keyword_{i}"):
                monitor.remove_keyword(keyword)
                st.rerun()

# Monitoring controls. Start and Stop button in the sidebar so users can control when the news monitoring is running
st.sidebar.subheader("Monitoring Control")
col1, col2 = st.sidebar.columns(2)

with col1:
    if st.button("🚀 Start", disabled=not monitor.urls or not monitor.keywords or monitor.running): #start button unclickable if no url, keyword or monitoring is already running
        if monitor.start_monitoring():
            st.session_state.monitoring_status = True
            st.sidebar.success("Monitoring started!")
            st.rerun()
        else:
            st.sidebar.error("Failed to start monitoring")

with col2:
    if st.button("🛑 Stop", disabled=not monitor.running):
        monitor.stop_monitoring()
        st.session_state.monitoring_status = False
        st.sidebar.success("Monitoring stopped!")
        st.rerun()

# Status indicator
if monitor.running:
    st.sidebar.success("🟢 Monitoring Active")
    st.sidebar.write(f"📊 Feeds: {len(monitor.urls)} | Keywords: {len(monitor.keywords)}") #tells how many RSS feeds and keywords is under current monitoring
else:
    st.sidebar.info("🔴 Monitoring Inactive")

# Main content area
col1, col2 = st.columns([2, 1])

with col1:
    st.header("📈 Live News Feed")

    # Check for new articles. Automatically show any new articles
    new_articles = monitor.get_new_articles() #check for new articles in the monitor's queue
    if new_articles:
        st.session_state.all_articles.extend(new_articles)#add if condition is complied

    # Manual scan button
    if st.button("🔍 Manual Scan Now"):
        with st.spinner("Scanning RSS feeds..."):
            new_articles = monitor.scan_for_news()
            if new_articles:
                st.session_state.all_articles.extend(new_articles)
                st.success(f"Found {len(new_articles)} new articles!")
            else:
                st.info("No new articles found")

    # Display articles
    if st.session_state.all_articles:
        st.subheader(f"📰 Found Articles ({len(st.session_state.all_articles)})")

        # Sort articles by timestamp (newest first)
        sorted_articles = sorted(st.session_state.all_articles,
                                    key=lambda x: x['timestamp'], reverse=True) #the newest articles will be on top

        for idx, article in enumerate(sorted_articles):
            with st.expander(f"📰 {article['title']} - {article['timestamp']}"):
                st.write(f"**🔗 URL:** {article['url']}")
                st.write(f"**📅 Time:** {article['timestamp']}")
                st.write(f"**🏷️ Keywords:** {', '.join(article['keywords'])}")
                st.write(f"**📝 Description:** {article.get('description', 'No description')}")
                st.write(f"**🌐 Source:** {article['source']}")

                if st.button("Open Article", key=f"open_{idx}"):
                    st.write(f"🔗 [Click here to open article]({article['url']})")
    else:
        st.info("No articles found yet. Add RSS feeds and keywords, then start monitoring!")

with col2:
    st.header("📊 Statistics")

    # Quick stats
    st.metric("RSS Feeds", len(monitor.urls))
    st.metric("Keywords", len(monitor.keywords))
    st.metric("Articles Found", len(st.session_state.all_articles))
    st.metric("Status", "🟢 Active" if monitor.running else "🔴 Inactive")

    # Clear articles button. Deletes all found articles and clears the monitor’s record of which articles it’s seen.
    if st.button("🗑️ Clear All Articles"):
        st.session_state.all_articles = []
        monitor.found_articles.clear()
        st.success("All articles cleared!")
        st.rerun()

# Footer
st.markdown("---")
st.markdown(
    "💡 **Tip:** Add RSS feeds from your favorite news sources and keywords you're interested in. The monitor will check every 10 minutes automatically!")

# Auto-refresh every 30 seconds when monitoring is active
if monitor.running:
    time.sleep(30)
    st.rerun()


