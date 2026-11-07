import sys

with open("dashboard.html", "r", encoding="utf-8") as f:
    text = f.read()

em_dash = "\u2014" in text
has_purple = "purple" in text.lower()
has_gradient = "gradient" in text.lower()
has_will_ship = "will ship" in text.lower()
has_reviews = "review" in text.lower() or "testimonial" in text.lower()
has_ai_tag = "made with ai" in text.lower()
has_favicon = 'rel="icon"' in text
has_privacy = "Privacy Policy" in text and "view-privacy" in text
has_terms = "Terms and Conditions" in text and "view-terms" in text
emojis = [c for c in text if ord(c) > 10000]

print("1. Em dash present:", em_dash)
print("2. Purple present:", has_purple)
print("3. Gradient present:", has_gradient)
print("4. Will ship present:", has_will_ship)
print("5. Reviews present:", has_reviews)
print("6. Made with AI tag present:", has_ai_tag)
print("7. Favicon present:", has_favicon)
print("8. Privacy policy present:", has_privacy)
print("9. Terms and conditions present:", has_terms)
print("10. Emojis found:", len(emojis), emojis)
