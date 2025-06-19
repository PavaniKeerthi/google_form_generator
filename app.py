import streamlit as st
import os
import google.auth
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google.generativeai as genai
import pandas as pd
import json
import time
import docx2txt
import PyPDF2
import re

# === CONFIGURATION ===
GEMINI_API_KEY = 'AIzaSyANSRsHPcgbEtlWUv3DBEfvVrUZjbGxx4c'  # <-- Replace here
SERVICE_ACCOUNT_FILE = 'service_account.json'

SCOPES = [
    'https://www.googleapis.com/auth/forms.body',
    'https://www.googleapis.com/auth/forms.responses.readonly',
    'https://www.googleapis.com/auth/drive.file'
]

# === GOOGLE AUTH ===
@st.cache_resource
def authenticate_google():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    service = build('forms', 'v1', credentials=creds)
    return service

# === FILE EXTRACTION ===
def extract_text(uploaded_file):
    file_extension = uploaded_file.name.split('.')[-1].lower()
    if file_extension == 'pdf':
        pdf_reader = PyPDF2.PdfReader(uploaded_file)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text() or ""
        return text
    elif file_extension == 'docx':
        return docx2txt.process(uploaded_file)
    elif file_extension == 'txt':
        return uploaded_file.read().decode('utf-8')
    else:
        return ""

# === GEMINI CONFIGURATION ===
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash-latest")

def clean_json_response(raw_text):
    cleaned = re.sub(r"```json|```", "", raw_text).strip()
    return json.loads(cleaned)

def generate_questions(text, num_questions, q_type):
    if q_type == 'MCQ':
        prompt = f"""
        Generate {num_questions} multiple-choice questions from this text. 
        Output strictly in this JSON format:
        {{
            "questions": [
                {{
                    "question": "...",
                    "options": ["opt1", "opt2", "opt3", "opt4"],
                    "answer": "correct option"
                }}
            ]
        }}
        Text: {text}
        """
    elif q_type == 'Blanks':
        prompt = f"""
        Generate {num_questions} fill-in-the-blank questions from this text.
        Output strictly in this JSON format:
        {{
            "questions": [
                {{
                    "question": "...",
                    "answer": "correct answer"
                }}
            ]
        }}
        Text: {text}
        """
    else:  # Mixed
        prompt = f"""
        Generate {num_questions} mixed questions (MCQs + Blanks).
        Output strictly in this JSON format:
        {{
            "questions": [
                {{
                    "type": "mcq",
                    "question": "...",
                    "options": ["opt1", "opt2", "opt3", "opt4"],
                    "answer": "correct option"
                }},
                {{
                    "type": "blank",
                    "question": "...",
                    "answer": "correct answer"
                }}
            ]
        }}
        Text: {text}
        """
    response = model.generate_content(prompt)
    return clean_json_response(response.text)

# === GOOGLE FORM CREATION ===
def create_google_form(service, form_title, questions, q_type):
    form = service.forms().create(body={"info": {"title": form_title}}).execute()
    form_id = form['formId']
    requests = []

    for idx, q in enumerate(questions['questions']):
        title = f"{idx+1}. {q['question']}"
        if (q_type == 'MCQ') or (q_type == 'Mixed' and q.get("type") == 'mcq'):
            options = [{"value": opt} for opt in q['options']]
            item = {
                "title": title,
                "questionItem": {
                    "question": {
                        "choiceQuestion": {
                            "type": "RADIO",
                            "options": options,
                            "shuffle": False
                        },
                        "required": True
                    }
                }
            }
        else:
            item = {
                "title": title,
                "questionItem": {
                    "question": {
                        "textQuestion": {"paragraph": False},
                        "required": True
                    }
                }
            }

        requests.append({
            "createItem": {
                "item": item,
                "location": {"index": idx}  # <-- The fix for your last error
            }
        })

    service.forms().batchUpdate(formId=form_id, body={"requests": requests}).execute()
    return form_id, f"https://docs.google.com/forms/d/{form_id}/viewform"

# === DOWNLOAD RESPONSES ===
def download_and_score(service, form_id, questions, q_type):
    responses = service.forms().responses().list(formId=form_id).execute()
    rows = []
    correct_answers = []

    for q in questions['questions']:
        if (q_type == 'MCQ') or (q_type == 'Mixed' and q.get("type") == 'mcq'):
            correct_answers.append(q['answer'])
        else:
            correct_answers.append(q['answer'].lower())

    for response in responses.get('responses', []):
        answers = response.get('answers', {})
        row = {}
        score = 0
        for i, ans in enumerate(answers.values()):
            user_ans = ans.get('textAnswers', {}).get('answers', [{}])[0].get('value', '')
            row[f'Q{i+1}'] = user_ans
            if user_ans.strip().lower() == correct_answers[i].strip().lower():
                score += 1
        row['Score'] = score
        rows.append(row)

    return pd.DataFrame(rows)

# === PRACTICE MODE WITH HARD TIMER ===
def practice_test(questions, timer_minutes, q_type):
    total_seconds = timer_minutes * 60
    start_time = time.time()

    if 'user_answers' not in st.session_state:
        st.session_state['user_answers'] = [''] * len(questions['questions'])
        st.session_state['test_active'] = True

    if st.session_state['test_active']:
        for idx, q in enumerate(questions['questions']):
            remaining = total_seconds - (time.time() - start_time)
            if remaining <= 0:
                st.warning("⏰ Time's up! Submitting automatically!")
                st.session_state['test_active'] = False
                break

            st.write(f"**{idx+1}. {q['question']}**")

            if (q_type == 'MCQ') or (q_type == 'Mixed' and q.get("type") == 'mcq'):
                st.session_state['user_answers'][idx] = st.radio(
                    "", q['options'], key=f"mcq_{idx}", index=q['options'].index(st.session_state['user_answers'][idx]) if st.session_state['user_answers'][idx] in q['options'] else 0)
            else:
                st.session_state['user_answers'][idx] = st.text_input(
                    "", value=st.session_state['user_answers'][idx], key=f"blank_{idx}")

            st.info(f"Remaining: {int(remaining//60)} min {int(remaining%60)} sec")

        if st.button("Submit Test"):
            st.session_state['test_active'] = False

    if not st.session_state['test_active']:
        calculate_score(questions, q_type)

def calculate_score(questions, q_type):
    score = 0
    for i, ua in enumerate(st.session_state['user_answers']):
        correct = questions['questions'][i]['answer']
        if ua.strip().lower() == correct.strip().lower():
            score += 1
    st.success(f"✅ Your Score: {score} / {len(questions['questions'])}")

# === STREAMLIT APP ===
st.set_page_config(page_title="AI Google Form Generator", page_icon="📝")
st.title("📝 AI Google Form Generator + Practice Mode + Timer")

uploaded_file = st.file_uploader("Upload File (PDF, DOCX, TXT)", type=["pdf", "docx", "txt"])
num_questions = st.number_input("Number of Questions", min_value=1, max_value=50, value=5)
form_title = st.text_input("Google Form Title", "Test Form")
q_type = st.selectbox("Question Type", ["MCQ", "Blanks", "Mixed"])
timer_minutes = st.number_input("Practice Timer (minutes)", min_value=1, max_value=120, value=10)

if 'questions_data' not in st.session_state:
    st.session_state['questions_data'] = None
if 'form_id' not in st.session_state:
    st.session_state['form_id'] = None

if st.button("Generate Google Form"):
    if uploaded_file:
        with st.spinner("Generating Questions..."):
            service = authenticate_google()
            text = extract_text(uploaded_file)
            questions = generate_questions(text, num_questions, q_type)
            form_id, form_url = create_google_form(service, form_title, questions, q_type)
            st.session_state['form_id'] = form_id
            st.session_state['questions_data'] = questions
            st.success("✅ Google Form Created Successfully!")
            st.write(f"[Open Google Form]({form_url})")
    else:
        st.warning("⚠ Please upload a file first!")

if st.session_state['form_id']:
    if st.button("Download Responses & Score"):
        service = authenticate_google()
        df = download_and_score(service, st.session_state['form_id'], st.session_state['questions_data'], q_type)
        if df.empty:
            st.info("No responses yet.")
        else:
            st.download_button("Download CSV", data=df.to_csv(index=False), file_name="responses.csv", mime="text/csv")

if st.session_state['questions_data']:
    st.subheader("🧪 Practice Test Mode with Timer")
    practice_test(st.session_state['questions_data'], timer_minutes, q_type)
