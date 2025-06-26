import os
import re
import json
import base64
import string
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from supabase import create_client, Client

# --- SETTINGS ---
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
FETCH_LIMIT = 200
TABLE_NAME = "master_contacts"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- REGEX ---
EMAIL_REGEX = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
PHONE_REGEX = r'(\+1[-.\s]?)?(\()?(\d{3})(\))?[-.\s]?(\d{3})[-.\s]?(\d{4})'
NAME_REGEX = r'(?:Full\s*Name|Name)\s*[:\-]?\s*([\w\s]{3,40})'
LISTING_URL_REGEX = r'(https://atmbrokerage\.com/atm-route-for-sale/[^\s]+)'

# --- AREA CODE MAP ---
area_df = pd.read_csv("area_codes.csv")
area_df['areacodenumber'] = area_df['areacodenumber'].astype(str).str.zfill(3)
area_code_map = area_df.set_index('areacodenumber')[['state', 'location']].to_dict(orient='index')

# --- IGNORE INTERNAL EMAILS ---
IGNORE_EMAILS = {
    "info@atmbrokerage.com", "info@connectatm.com",
    "interest@bizbuysell.com", "docs@email.pandadoc.net",
    "noreply@gohighlevel.com", "welcome@supabase.com"
}

# --- CLEANER ---
def clean(val):
    if isinstance(val, str):
        # Replace all whitespace characters (including newlines, tabs, etc.) with a single space
        val = re.sub(r'\s+', ' ', val).strip()
        # Ensure only printable ASCII characters remain
        val = ''.join(ch for ch in val if ch in string.printable)
        return val
    return val

# --- AUTH ---
def authenticate_gmail():
    creds = None
    creds_env = os.getenv("GMAIL_CREDENTIALS_JSON")
    token_env = os.getenv("GMAIL_TOKEN_JSON")

    if creds_env and token_env:
        print("🔐 Using GitHub Secrets for authentication")
        token_info = json.loads(token_env)
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
    else:
        print("🔐 Using local credentials.json/token.json")
        if os.path.exists('token.json'):
            creds = Credentials.from_authorized_user_file('token.json', SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
                creds = flow.run_local_server(port=0)
            with open('token.json', 'w') as token:
                token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)

# --- FETCH ---
def fetch_messages(service, limit):
    all_msgs = []
    next_page_token = None
    while len(all_msgs) < limit:
        kwargs = {'userId': 'me', 'maxResults': 500}
        if next_page_token:
            kwargs['pageToken'] = next_page_token
        resp = service.users().messages().list(**kwargs).execute()
        all_msgs.extend(resp.get('messages', []))
        next_page_token = resp.get('nextPageToken')
        if not next_page_token:
            break
    return all_msgs[:limit]

# --- HELPERS ---
def normalize_phone(match):
    try:
        return f"{match[2]}{match[4]}{match[5]}"
    except:
        return ''

def map_area(phone):
    if len(phone) >= 10:
        area = phone[:3]
        return area_code_map.get(area, {'state': '', 'location': ''})
    return {'state': '', 'location': ''}

def get_body_text(payload):
    if 'parts' in payload:
        for part in payload['parts']:
            mime = part.get('mimeType', '')
            data = part.get('body', {}).get('data', '')
            if not data:
                continue
            try:
                decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='ignore').strip()
                if mime == 'text/plain':
                    return decoded
                elif mime == 'text/html':
                    return BeautifulSoup(decoded, 'html.parser').get_text(separator=' ', strip=True)
            except:
                continue
    else:
        data = payload.get('body', {}).get('data', '')
        if data:
            try:
                decoded = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='ignore').strip()
                return BeautifulSoup(decoded, 'html.parser').get_text(separator=' ', strip=True)
            except:
                pass
    return ''

# --- SYNC ---
def sync_to_supabase(records):
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    for lead in records:
        email_val = lead.get("email", "").strip().lower()
        if not email_val or email_val in IGNORE_EMAILS:
            print(f"❌ Skipping internal or blank email: {email_val}")
            continue
        try:
            supabase.table(TABLE_NAME).upsert(lead, on_conflict=["email"]).execute()
            print(f"✅ Synced: {email_val}")
        except Exception as e:
            print(f"❌ Error inserting {email_val}: {e}")

# --- MAIN ---
def extract_and_sync(service):
    messages = fetch_messages(service, FETCH_LIMIT)
    print(f"📥 Pulled {len(messages)} messages")

    leads = []
    for idx, msg in enumerate(messages, 1):
        try:
            meta = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        except Exception as e:
            print(f"⚠️ Skipping message {msg['id']} due to error: {e}")
            continue

        payload = meta.get('payload', {})
        headers = payload.get('headers', [])
        date_str = datetime.fromtimestamp(int(meta.get('internalDate', '0')) // 1000).isoformat()

        from_header = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
        email_matches = re.findall(EMAIL_REGEX, from_header)
        email = email_matches[0] if email_matches else ''
        name_match = re.match(r'^(.*)<', from_header)
        name = name_match.group(1).strip().strip('"') if name_match else from_header.strip()

        body_text = get_body_text(payload)
        phone = ''
        listing_url = ''

        if body_text:
            em = re.search(EMAIL_REGEX, body_text)
            if em: email = em.group(0).strip()

            nm = re.search(NAME_REGEX, body_text, re.IGNORECASE)
            if nm: name = nm.group(1).strip().title()

            phone_matches = re.findall(PHONE_REGEX, body_text)
            phones = [normalize_phone(p) for p in phone_matches if normalize_phone(p)]
            if phones: phone = phones[0]

            listing_match = re.search(LISTING_URL_REGEX, body_text)
            if listing_match: listing_url = listing_match.group(1).strip()

        if not name and email:
            local = email.split('@')[0]
            guess = local.replace('.', ' ').replace('_', ' ')
            if len(guess.split()) >= 2:
                name = guess.title()

        area_info = map_area(phone)
        state = area_info['state']
        city = area_info['location']

        # Sanitize all extracted values using the improved clean function
        email = clean(email)
        name = clean(name)
        phone = clean(phone)
        listing_url = clean(listing_url)
        body_text = clean(body_text)

        if not email:
            continue

        leads.append({
            'email': email,
            'name': name,
            'phone': phone,
            'state': state,
            'location': city,
            'date': date_str,
            'message': body_text[:500],
            'source_url': clean(listing_url) if listing_url else None,
            'message_id': msg['id']
        })

        if idx % 500 == 0:
            print(f"✅ Processed {idx} messages")

    print(f"📤 Syncing {len(leads)} contacts to Supabase...")
    sync_to_supabase(leads)

# --- ENTRY ---
if __name__ == '__main__':
    print("🔐 Authenticating Gmail...")
    service = authenticate_gmail()
    print("📬 Extracting + syncing contacts...")
    extract_and_sync(service)
