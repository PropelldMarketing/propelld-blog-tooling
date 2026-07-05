"""
Link parsing + insertion helpers for blog post HTML.

Handles both body halves separately (post-body + post-body-2nd-half).
Uses BeautifulSoup for reliable HTML manipulation.
"""

import re
from typing import Optional
from bs4 import BeautifulSoup


INTERNAL_HOSTS = ("propelld.com", "www.propelld.com")


def is_internal(url: str) -> bool:
    """Is this URL internal to propelld.com?"""
    if not url:
        return False
    if url.startswith("/"):
        return True
    return any(h in url for h in INTERNAL_HOSTS)


def normalize_url(url: str) -> str:
    """Strip protocol, host, query, hash. Return path only."""
    if not url:
        return ""
    # Strip protocol + host
    url = re.sub(r"^https?://(www\.)?propelld\.com", "", url)
    # Strip query + hash
    url = url.split("?")[0].split("#")[0]
    # Ensure leading slash
    if not url.startswith("/"):
        url = "/" + url
    # Strip trailing slash for consistency (except root)
    if len(url) > 1:
        url = url.rstrip("/")
    return url


def extract_links(html: str) -> list:
    """Return list of (anchor_text, href, position) tuples for internal links."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for i, a in enumerate(soup.find_all("a", href=True)):
        href = a["href"]
        if is_internal(href):
            links.append({
                "anchor": a.get_text(strip=True),
                "href": normalize_url(href),
                "raw_href": href,
                "position": i,
            })
    return links


def find_position_marker(html: str, position: str) -> Optional[int]:
    """
    Locate a body position for link insertion. Positions:
      - "intro"         : end of first <p>
      - "first-h2"      : end of first <p> after first <h2>
      - "mid"           : middle <p>
      - "pre-conclusion": end of second-to-last <p>
      - "conclusion"    : end of last <p>
    Returns character offset in html, or None.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    paras = soup.find_all("p")
    if not paras:
        return None

    target = None
    if position == "intro":
        target = paras[0]
    elif position == "first-h2":
        h2s = soup.find_all("h2")
        if h2s:
            after = h2s[0].find_next("p")
            target = after or paras[0]
        else:
            target = paras[0]
    elif position == "mid":
        target = paras[len(paras) // 2]
    elif position == "pre-conclusion":
        target = paras[-2] if len(paras) >= 2 else paras[-1]
    elif position == "conclusion":
        target = paras[-1]
    if target is None:
        return None

    # Return string offset of the target's closing </p>
    marker = str(target)
    return html.find(marker) + len(marker)


def insert_link_in_body(html: str, anchor_text: str, href: str,
                        position: str = "mid") -> str:
    """
    Insert an <a href="{href}">{anchor_text}</a> link at the given position.
    If anchor_text already exists as plain text at that position, wrap it.
    Otherwise, append a sentence linking to href just before that position's end.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    paras = soup.find_all("p")
    if not paras:
        return html

    if position == "intro":
        target = paras[0]
    elif position == "first-h2":
        h2s = soup.find_all("h2")
        target = h2s[0].find_next("p") if h2s else paras[0]
    elif position == "mid":
        target = paras[len(paras) // 2]
    elif position == "pre-conclusion":
        target = paras[-2] if len(paras) >= 2 else paras[-1]
    elif position == "conclusion":
        target = paras[-1]
    else:
        target = paras[len(paras) // 2]

    # If anchor text already exists in this paragraph as plain text, wrap it
    text_content = target.get_text()
    if anchor_text.lower() in text_content.lower():
        # Find the text node and wrap the matching substring
        for text_node in target.find_all(string=True):
            if anchor_text.lower() in text_node.lower():
                idx = text_node.lower().find(anchor_text.lower())
                before, match, after = (
                    text_node[:idx],
                    text_node[idx:idx + len(anchor_text)],
                    text_node[idx + len(anchor_text):]
                )
                new_a = soup.new_tag("a", href=href)
                new_a.string = match
                text_node.replace_with(before, new_a, after)
                return str(soup)
    # Fallback: append a sentence to the target paragraph
    sep = " " if target.get_text(strip=True) else ""
    new_a = soup.new_tag("a", href=href)
    new_a.string = anchor_text
    target.append(sep)
    target.append("For more on ")
    target.append(new_a)
    target.append(", see the guide.")
    return str(soup)


def link_count(html: str) -> int:
    if not html:
        return 0
    return len(extract_links(html))
