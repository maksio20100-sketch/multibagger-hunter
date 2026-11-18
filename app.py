"""Multibagger Hunter - entry point of the Streamlit website.

Streamlit runs this file from top to bottom every time a user opens the
page or interacts with it. Each `st.something(...)` call adds an element
(title, text, message, ...) to the web page.
"""

import streamlit as st

# set_page_config must be the FIRST Streamlit call. It sets the browser tab
# title and icon, and the page layout.
st.set_page_config(
    page_title="Multibagger Hunter",
    page_icon="🎯",
    layout="centered",
)

# The big heading at the top of the page.
st.title("Multibagger Hunter")

# A smaller line of text under the title.
st.subheader("Multibagger Hunter — Stock Discovery Engine")

# A blue info box.
st.info("Scanner coming soon.")
