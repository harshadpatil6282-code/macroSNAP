import json
import re
import time
from datetime import datetime

import streamlit as st
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="MacroSnap",
    page_icon="🥗",
    layout="centered",
    initial_sidebar_state="expanded",
)


# ============================================================
# IMPORT PROMPTS
# ============================================================

from prompts import (
    SYSTEM_PROMPT,
    SUMMARY_REQUEST_PROMPT,
    WELCOME_MESSAGE_TEMPLATE,
)


# ============================================================
# CONFIGURATION
# ============================================================

MODEL_CANDIDATES = [
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

MODEL_NAME = MODEL_CANDIDATES[0]


# ============================================================
# LOAD SECRETS
# ============================================================

try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]

    TWILIO_ACCOUNT_SID = st.secrets.get(
        "TWILIO_ACCOUNT_SID",
        ""
    )

    TWILIO_AUTH_TOKEN = st.secrets.get(
        "TWILIO_AUTH_TOKEN",
        ""
    )

    TWILIO_WHATSAPP_FROM = st.secrets.get(
        "TWILIO_WHATSAPP_FROM",
        "whatsapp:+14155238886"
    )

    TWILIO_CONTENT_SID = st.secrets.get(
        "TWILIO_CONTENT_SID",
        ""
    )

except Exception:
    st.error(
        "API configuration is missing. "
        "Please configure .streamlit/secrets.toml."
    )
    st.stop()


# ============================================================
# CLIENTS
# ============================================================

@st.cache_resource
def get_gemini_client():

    return genai.Client(
        api_key=GEMINI_API_KEY
    )


@st.cache_resource
def get_twilio_client():

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return None

    return TwilioClient(
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN
    )


gemini_client = get_gemini_client()
twilio_client = get_twilio_client()


# ============================================================
# SESSION STATE
# ============================================================

def initialize_session():

    defaults = {
        "onboarded": False,
        "name": "",
        "whatsapp_number": "",
        "chat": None,
        "messages": [],
        "meal_count": 0,
        "total_messages": 0,
        "current_model": MODEL_NAME,
    }

    for key, value in defaults.items():

        if key not in st.session_state:
            st.session_state[key] = value


initialize_session()


# ============================================================
# CUSTOM CSS
# ============================================================

st.markdown(
    """
    <style>

    .main-title {
        text-align: center;
        font-size: 42px;
        font-weight: 800;
        margin-bottom: 0;
    }

    .subtitle {
        text-align: center;
        color: #777;
        font-size: 16px;
        margin-bottom: 25px;
    }

    .nutrition-card {
        padding: 18px;
        border-radius: 15px;
        border: 1px solid rgba(128,128,128,0.25);
        margin-bottom: 15px;
    }

    .small-text {
        color: #777;
        font-size: 13px;
    }

    </style>
    """,
    unsafe_allow_html=True
)


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def normalize_phone_number(phone):

    phone = phone.strip()

    # Keep + and numbers only
    phone = re.sub(r"[^\d+]", "", phone)

    if not phone.startswith("+"):
        return None

    if len(phone) < 10:
        return None

    return phone


def clean_whatsapp_text(text):

    if not text:
        return "No nutrition summary available."

    text = text.strip()

    # Remove markdown formatting
    text = text.replace("**", "")
    text = text.replace("__", "")

    # Collapse multiple spaces/newlines
    text = " ".join(text.split())

    # WhatsApp message safety limit for this application
    if len(text) > 1500:
        text = text[:1500] + "..."

    return text


# ============================================================
# MESSAGE RENDERING
# ============================================================

def render_message(message):

    role = message["role"]

    with st.chat_message(role):

        if message["kind"] == "text":

            st.write(message["content"])

        elif message["kind"] == "image":

            st.image(
                message["content"],
                width="stretch"
            )


def add_message(role, kind, content):

    message = {
        "role": role,
        "kind": kind,
        "content": content,
    }

    st.session_state.messages.append(message)

    render_message(message)


# ============================================================
# GEMINI
# ============================================================

def create_chat(model_name=None):

    model_name = model_name or st.session_state.get(
        "current_model",
        MODEL_NAME,
    )

    return gemini_client.chats.create(
        model=model_name,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.4,
        ),
    )


def get_next_model_name(current_model_name):

    if current_model_name in MODEL_CANDIDATES:
        current_index = MODEL_CANDIDATES.index(current_model_name)
        next_index = current_index + 1
    else:
        next_index = 1

    if next_index < len(MODEL_CANDIDATES):
        return MODEL_CANDIDATES[next_index]

    return MODEL_CANDIDATES[0]


def ask_gemini(parts, retries=3):

    last_error = None
    current_model = st.session_state.get("current_model", MODEL_NAME)

    for attempt in range(retries + 1):

        try:

            if st.session_state.chat is None or st.session_state.get("current_model") != current_model:
                st.session_state.chat = create_chat(current_model)
                st.session_state.current_model = current_model

            response = st.session_state.chat.send_message(parts)

            if response is None:
                return "I couldn't generate a response."

            if not response.text:
                return "I couldn't understand that input."

            return response.text

        except Exception as error:

            last_error = error
            error_text = str(error).upper()

            if (
                "503" in str(error)
                or "UNAVAILABLE" in error_text
                or "429" in str(error)
                or "RESOURCE_EXHAUSTED" in error_text
            ):

                if attempt < retries:
                    next_model = get_next_model_name(current_model)
                    st.session_state.current_model = next_model
                    st.session_state.chat = create_chat(next_model)
                    current_model = next_model
                    time.sleep(2)
                    continue

            break

    return (
        "Sorry, I couldn't process that request.\n\n"
        f"Technical details: {last_error}"
    )


# ============================================================
# WHATSAPP
# ============================================================

def send_whatsapp(
    to_number,
    user_name,
    summary
):

    if twilio_client is None:

        return (
            False,
            "Twilio credentials are not configured."
        )

    if not TWILIO_CONTENT_SID:

        return (
            False,
            "TWILIO_CONTENT_SID is missing."
        )

    try:

        content_variables = json.dumps(
            {
                "1": user_name,
                "2": clean_whatsapp_text(summary),
            },
            ensure_ascii=False,
        )

        message = twilio_client.messages.create(

            from_=TWILIO_WHATSAPP_FROM,

            to=f"whatsapp:{to_number}",

            content_sid=TWILIO_CONTENT_SID,

            content_variables=content_variables,
        )

        return True, message.sid

    except Exception as error:

        return False, str(error)


def show_summary_in_app(user_name, summary):

    st.subheader("📄 Nutrition summary preview")

    st.code(
        f"{user_name}\n\n{clean_whatsapp_text(summary)}",
        language="text"
    )

    return True


# ============================================================
# SIDEBAR
# ============================================================

def render_sidebar():

    with st.sidebar:

        st.header("🥗 MacroSnap")

        if st.session_state.onboarded:

            st.success(
                f"Logged in as {st.session_state.name}"
            )

            st.divider()

            st.metric(
                "Meals analyzed",
                st.session_state.meal_count
            )

            st.metric(
                "Messages",
                st.session_state.total_messages
            )

            st.divider()

            st.caption(
                f"WhatsApp: "
                f"{st.session_state.whatsapp_number}"
            )

            st.divider()

            if st.button(
                "🔄 Start New Session",
                width="stretch"
            ):

                for key in [
                    "onboarded",
                    "name",
                    "whatsapp_number",
                    "chat",
                    "messages",
                    "meal_count",
                    "total_messages",
                ]:

                    if key == "onboarded":
                        st.session_state[key] = False

                    elif key in ["name", "whatsapp_number"]:
                        st.session_state[key] = ""

                    elif key == "messages":
                        st.session_state[key] = []

                    elif key == "meal_count":
                        st.session_state[key] = 0

                    elif key == "total_messages":
                        st.session_state[key] = 0

                    else:
                        st.session_state[key] = None

                st.rerun()

        else:

            st.info(
                "Enter your details to start "
                "your AI nutrition session."
            )

        st.divider()

        st.caption(
            "MacroSnap provides estimated nutrition "
            "information and is not a medical diagnostic tool."
        )


render_sidebar()


# ============================================================
# ONBOARDING
# ============================================================

if not st.session_state.onboarded:

    st.markdown(
        '<div class="main-title">🥗 MacroSnap</div>',
        unsafe_allow_html=True
    )

    st.markdown(
        '<div class="subtitle">'
        'Snap it. Track it. Understand it.'
        '</div>',
        unsafe_allow_html=True
    )

    st.markdown(
        "### 👋 Welcome!"
    )

    st.write(
        "Enter your details once and start chatting "
        "with your AI nutrition buddy."
    )

    with st.form("onboarding_form"):

        name = st.text_input(
            "Your name",
            placeholder="Harshad"
        )

        whatsapp_number = st.text_input(
            "WhatsApp number",
            placeholder="+91XXXXXXXXXX",
            help="Enter the number with country code."
        )

        submitted = st.form_submit_button(
            "Let's Get Started 🚀",
            width="stretch"
        )

    if submitted:

        if not name.strip():

            st.warning(
                "Please enter your name."
            )

            st.stop()

        normalized_phone = normalize_phone_number(
            whatsapp_number
        )

        if normalized_phone is None:

            st.warning(
                "Enter a valid WhatsApp number "
                "with country code, e.g. +919876543210."
            )

            st.stop()

        try:

            st.session_state.name = name.strip()

            st.session_state.whatsapp_number = normalized_phone

            st.session_state.current_model = MODEL_NAME
            st.session_state.chat = create_chat(MODEL_NAME)

            st.session_state.messages = []

            st.session_state.meal_count = 0

            st.session_state.total_messages = 0

            st.session_state.onboarded = True

            st.rerun()

        except Exception as error:

            st.error(
                f"Could not initialize Gemini: {error}"
            )

    st.stop()


# ============================================================
# MAIN HEADER
# ============================================================

header_col, button_col = st.columns(
    [5, 2],
    vertical_alignment="center"
)


with header_col:

    st.markdown(
        '<div class="main-title">🥗 MacroSnap</div>',
        unsafe_allow_html=True
    )


with button_col:

    send_disabled = (
        len(st.session_state.messages) <= 1
    )

    if st.button(
        "📤 Send to WhatsApp",
        disabled=send_disabled,
        width="stretch"
    ):

        with st.spinner(
            "Creating your nutrition summary..."
        ):

            summary = ask_gemini(
                [SUMMARY_REQUEST_PROMPT]
            )

        if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_CONTENT_SID:

            show_summary_in_app(
                st.session_state.name,
                summary,
            )

            st.info(
                "WhatsApp sending is disabled for this demo because Twilio template credentials are not configured. "
                "Your summary is shown here instead."
            )

        else:

            with st.spinner(
                "Sending to WhatsApp..."
            ):

                success, info = send_whatsapp(
                    st.session_state.whatsapp_number,
                    st.session_state.name,
                    summary
                )

            if success:

                st.success(
                    "Nutrition summary sent to WhatsApp! 📲"
                )

            else:

                st.warning(
                    "Twilio WhatsApp send failed. Showing the summary in-app instead."
                )

                show_summary_in_app(
                    st.session_state.name,
                    summary,
                )

                st.error(
                    f"Technical details: {info}"
                )


st.caption(
    f"Logged in as {st.session_state.name} • "
    f"WhatsApp: {st.session_state.whatsapp_number}"
)


# ============================================================
# WELCOME MESSAGE
# ============================================================

if not st.session_state.messages:

    welcome = WELCOME_MESSAGE_TEMPLATE.format(
        name=st.session_state.name
    )

    add_message(
        "assistant",
        "text",
        welcome
    )

else:

    for message in st.session_state.messages:

        render_message(message)


# ============================================================
# CHAT INPUT
# ============================================================

user_input = st.chat_input(
    "Ask about your food or attach a meal photo...",
    accept_file=True,
    file_type=[
        "jpg",
        "jpeg",
        "png",
        "webp"
    ],
)


# ============================================================
# PROCESS USER INPUT
# ============================================================

if user_input:

    photo = (
        user_input.files[0]
        if user_input.files
        else None
    )

    text = user_input.text.strip()

    parts = []

    # --------------------------------------------------------
    # IMAGE
    # --------------------------------------------------------

    if photo is not None:

        try:

            photo_bytes = photo.getvalue()

            add_message(
                "user",
                "image",
                photo_bytes
            )

            image_part = types.Part.from_bytes(
                data=photo_bytes,
                mime_type=photo.type
            )

            parts.append(image_part)

            st.session_state.meal_count += 1

        except Exception as error:

            st.error(
                f"Could not read the image: {error}"
            )

            st.stop()


    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    if text:

        add_message(
            "user",
            "text",
            text
        )

        parts.append(text)

    elif photo is not None:

        parts.append(
            """
            Analyze this meal image.

            Identify the visible foods.

            Estimate:
            - calories
            - protein
            - carbohydrates
            - fat

            Explain that these are approximate values.
            Consider typical portion sizes.
            """
        )


    # --------------------------------------------------------
    # SEND TO GEMINI
    # --------------------------------------------------------

    if parts:

        with st.spinner(
            "🔎 Analyzing your meal..."
        ):

            answer = ask_gemini(parts)

        add_message(
            "assistant",
            "text",
            answer
        )

        st.session_state.total_messages += 1