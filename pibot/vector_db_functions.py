"""Reusable Wikipedia and Wikidata Vector Database functions."""

import time

import requests

VECTORDB_URL = "https://wd-vectordb.wmcloud.org"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = (
	"Pi bot VectorDB integration "
	"(https://www.wikidata.org/wiki/User:Pi_bot)"
)
MAX_ATTEMPTS = 3


def _get_with_retry(url, params):
	"""Retry rate limits and temporary request failures three times."""
	attempt = 0
	while attempt < MAX_ATTEMPTS:
		attempt += 1
		try:
			response = requests.get(
				url,
				params=params,
				headers={"User-Agent": USER_AGENT},
			)
			response.raise_for_status()
			return response
		except requests.RequestException as e:
			if attempt >= MAX_ATTEMPTS:
				raise

			delay = (
				e.response.headers.get("Retry-After", 1)
				if e.response is not None
				else 1
			)
			time.sleep(float(delay))


def get_query_text(page, text=None):
	"""Return supplied text or retrieve the page's plain-text introduction."""
	if text and text.strip():
		return text.strip()

	language = page.site.code
	page_title = page.title()
	url = f"https://{language}.wikipedia.org/w/api.php"

	response = _get_with_retry(
		url,
		{
			"action": "query",
			"format": "json",
			"formatversion": 2,
			"prop": "extracts",
			"exintro": 1,
			"explaintext": 1,
			"titles": page_title,
		},
	)
	data = response.json()

	pages = data.get("query", {}).get("pages", [])
	if pages:
		extract = pages[0].get("extract", "").strip()
		if extract:
			return extract

	# Fallback to the page title if no extract is found
	return page_title


def filter_candidates_by_sitelink(page, candidates):
	"""Remove candidates already linked to the investigated Wikipedia."""
	if not candidates:
		return []

	language = page.site.code
	wiki_site = f"{language}wiki"

	response = _get_with_retry(
		WIKIDATA_API_URL,
		{
			"action": "wbgetentities",
			"format": "json",
			"ids": "|".join(candidate["QID"] for candidate in candidates),
			"props": "sitelinks",
			"sitefilter": wiki_site,
		},
	)
	data = response.json()

	entities = data.get("entities", {})
	filtered_candidates = []
	for candidate in candidates:
		qid = candidate["QID"]
		entity = entities.get(qid)
		if not entity or not isinstance(entity, dict):
			continue
		if wiki_site not in entity.get("sitelinks", {}):
			filtered_candidates.append(candidate)
	return filtered_candidates


def vector_search(page, text=None):
	"""Search VectorDB and exclude candidates with a same-language sitelink."""
	query_text = get_query_text(page, text)
	url = VECTORDB_URL + "/item/query/"

	response = _get_with_retry(
		url,
		{
			"query": query_text,
			"lang": "all",
			"K": 50,
			"rerank": False,
			"scope": "all",
		},
	)
	data = response.json()

	data.sort(key=lambda result: result["similarity_score"], reverse=True)
	return filter_candidates_by_sitelink(page, data)


def get_similarity_scores(page, qids, text=None):
	"""Calculate similarity scores for specific Wikidata QIDs."""
	qids = list(dict.fromkeys(qids))
	if not qids:
		return {}

	query_text = get_query_text(page, text)
	url = VECTORDB_URL + "/similarity-score/"
	scores = {}

	for start in range(0, len(qids), 100):
		response = _get_with_retry(
			url,
			{
			"query": query_text,
			"qid": ",".join(qids[start : start + 100]),
			"lang": "all",
			},
		)
		data = response.json()

		scores.update(
			{result["QID"]: result["similarity_score"] for result in data}
		)
	return scores
