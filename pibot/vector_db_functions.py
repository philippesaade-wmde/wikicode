"""Reusable Wikipedia and Wikidata Vector Database functions."""

import time
from urllib.parse import quote_plus

import requests

VECTORDB_URL = "https://wd-vectordb.wmcloud.org"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
USER_AGENT = (
	"Pi bot VectorDB integration "
	"(https://www.wikidata.org/wiki/User:Pi_bot)"
)
MAX_ATTEMPTS = 3
MAX_ENCODED_QUERY_LENGTH = 4000
CATEGORY_CLASS = "Q59541917"
DISAMBIGUATION_CLASS = "Q4167410"


def _get_with_retry(url, params, max_attempts=MAX_ATTEMPTS):
	"""Retry request failures up to the specified number of attempts."""
	attempt = 0
	while attempt < max_attempts:
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
			if attempt >= max_attempts:
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
		query_text = text.strip()
	else:
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
		query_text = pages[0].get("extract", "").strip() if pages else ""
		if not query_text:
			query_text = page_title

	encoded_length = 0
	for index, character in enumerate(query_text):
		encoded_length += len(quote_plus(character))
		if encoded_length > MAX_ENCODED_QUERY_LENGTH:
			return query_text[:index].rstrip()

	return query_text


def filter_candidates(page, candidates, instanceof=None):
	"""Remove candidates with a same-language sitelink or wrong class."""
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
			"props": "sitelinks|claims" if instanceof else "sitelinks",
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
			if instanceof:
				claims = entity.get("claims", {}).get("P31", [])
				instance_qids = {
					claim.get("mainsnak", {})
					.get("datavalue", {})
					.get("value", {})
					.get("id")
					for claim in claims
				}
				if instanceof not in instance_qids:
					continue
			filtered_candidates.append(candidate)
	return filtered_candidates


def vector_search(page, text=None):
	"""Search VectorDB and exclude candidates with a same-language sitelink."""
	query_text = get_query_text(page, text)
	url = VECTORDB_URL + "/item/query/"
	params = {
		"query": query_text,
		"lang": "all",
		"K": 50,
		"rerank": False,
		"scope": "all",
	}

	instanceof = None
	if page.namespace() == 14:
		instanceof = CATEGORY_CLASS
	elif page.isDisambig():
		instanceof = DISAMBIGUATION_CLASS

	if instanceof:
		params["instanceof"] = instanceof
		try:
			response = _get_with_retry(url, params, max_attempts=1)
		except requests.RequestException:
			params.pop("instanceof")
			response = _get_with_retry(url, params)
	else:
		response = _get_with_retry(url, params)
	data = response.json()

	data.sort(key=lambda result: result["similarity_score"], reverse=True)
	return filter_candidates(page, data, instanceof)


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


def process_vector_data(mydb, mycursor, page, title, language):
	"""Calculate candidate scores and add the best vector search result."""
	success = True
	try:
		query_text = get_query_text(page)
	except requests.RequestException:
		return False

	# Calculate similarity scores for existing open candidates.
	mycursor.execute(
		"SELECT 1 FROM newarticles WHERE candidate = %s AND site = %s "
		"AND done = 0 AND similarity_score IS NULL LIMIT 1",
		(title, language),
	)
	if mycursor.fetchone():
		mycursor.execute(
			"SELECT DISTINCT qid FROM newarticles "
			"WHERE candidate = %s AND site = %s AND done = 0 "
			"AND similarity_score IS NULL",
			(title, language),
		)
		qids = [row[0] for row in mycursor.fetchall()]
		try:
			scores = get_similarity_scores(page, qids, text=query_text)
		except requests.RequestException:
			success = False
		else:
			for qid, score in scores.items():
				mycursor.execute(
					"UPDATE newarticles SET similarity_score = %s "
					"WHERE qid = %s AND candidate = %s AND site = %s",
					(score, qid, title, language),
				)
			mydb.commit()

	# Add the top 1 vector search result.
	mycursor.execute(
		"SELECT 1 FROM newarticles WHERE candidate = %s AND site = %s "
		"AND `source` IN (2, 3) LIMIT 1",
		(title, language),
	)
	if not mycursor.fetchone():
		try:
			vector_results = vector_search(page, text=query_text)
		except requests.RequestException:
			success = False
		else:
			if vector_results:
				vector_result = vector_results[0]
				vector_qid = vector_result["QID"]
				vector_score = vector_result["similarity_score"]
				mycursor.execute(
					"SELECT 1 FROM newarticles "
					"WHERE qid = %s AND candidate = %s AND site = %s LIMIT 1",
					(vector_qid, title, language),
				)
				if mycursor.fetchone():
					mycursor.execute(
						"UPDATE newarticles SET `source` = `source` | 2, "
						"similarity_score = %s "
						"WHERE qid = %s AND candidate = %s AND site = %s",
						(vector_score, vector_qid, title, language),
					)
					# Reopen candidates that Pi bot previously closed automatically.
					mycursor.execute(
						"UPDATE newarticles SET done = 0, decision = 0 "
						"WHERE qid = %s AND candidate = %s AND site = %s "
						"AND done = 1 AND decision = 1 AND user = %s",
						(vector_qid, title, language, "NA"),
					)
				else:
					mycursor.execute(
						"INSERT INTO newarticles "
						"(qid, candidate, site, `source`, similarity_score) "
						"VALUES (%s, %s, %s, 2, %s)",
						(vector_qid, title, language, vector_score),
					)
				mydb.commit()

	return success
