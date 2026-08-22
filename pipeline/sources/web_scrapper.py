import requests

def scrape_web_data(url):
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            