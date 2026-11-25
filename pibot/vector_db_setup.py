"""Prepare the Pi bot database and enrich its open candidate backlog."""

import mysql.connector
import pywikibot
import requests
from database_login import (
	database_database,
	database_host,
	database_password,
	database_user,
)
from pywikibot.exceptions import NoPageError
from vector_db_functions import (
	get_query_text,
	get_similarity_scores,
	vector_search,
)

BATCH_SIZE = 100
PRINT_EVERY = 200

def process_page(mydb, mycursor, page, title, language):
	'''Process a single Wikipedia page to calculate similarity scores and perform vector search.'''

	success = True
	try:
		query_text = get_query_text(page)
	except requests.RequestException:
		return False

	# Calculate similarity scores between the Wikipedia article and the Wikidata items.
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

	# Perform vector search on the Wikipedia article.
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


def main():

	'''Setup database for vector search and add missing columns.'''
	mydb = mysql.connector.connect(
		host=database_host,
		user=database_user,
		passwd=database_password,
		database=database_database,
	)
	mycursor = mydb.cursor()

	mycursor.execute(
		"SELECT COUNT(*) FROM information_schema.COLUMNS "
		"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'newarticles' "
		"AND COLUMN_NAME = 'source'",
		(database_database,),
	)
	if mycursor.fetchone()[0] == 0:
		# Add 'source' column: 1: WD API, 2: Vector API, 3: WD and Vector API
		mycursor.execute(
			"ALTER TABLE newarticles ADD COLUMN `source` TINYINT UNSIGNED "
			"NOT NULL DEFAULT 1 COMMENT '1=WD API, 2=Vector API, 3=WD + Vector API'"
		)

	mycursor.execute(
		"SELECT COUNT(*) FROM information_schema.COLUMNS "
		"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'newarticles' "
		"AND COLUMN_NAME = 'similarity_score'",
		(database_database,),
	)
	if mycursor.fetchone()[0] == 0:
		# Add 'similarity_score' column calculated from the Vector API
		mycursor.execute(
			"ALTER TABLE newarticles ADD COLUMN similarity_score DOUBLE NULL"
		)

	mydb.commit()

	'''Process pages that still need similarity scores or vector search.'''
	backlog_db = mysql.connector.connect(
		host=database_host,
		user=database_user,
		passwd=database_password,
		database=database_database,
	)
	backlog_cursor = backlog_db.cursor()
	backlog_cursor.execute(
		"SELECT DISTINCT open_row.candidate, open_row.site "
		"FROM newarticles AS open_row "
		"WHERE open_row.done = 0 AND ("
		"open_row.similarity_score IS NULL OR NOT EXISTS ("
		"SELECT 1 FROM newarticles AS vector_row "
		"WHERE vector_row.candidate = open_row.candidate "
		"AND vector_row.site = open_row.site "
		"AND vector_row.`source` IN (2, 3)))"
	)

	sites = {}
	count = 0
	success_count = 0
	while True:
		pending_pages = backlog_cursor.fetchmany(BATCH_SIZE)
		if not pending_pages:
			break

		for title, language in pending_pages:
			try:
				if language not in sites:
					sites[language] = pywikibot.Site(language, "wikipedia")
				page = pywikibot.Page(sites[language], title)
				pywikibot.ItemPage.fromPage(page).get()
			except NoPageError:
				# No linked item was found, so process the page.
				pass
			except pywikibot.Error:
				continue
			else:
				# A linked item was found, so no vector processing is needed.
				continue

			count += 1
			success = process_page(
				mydb,
				mycursor,
				page,
				title,
				language,
			)
			success_count += 1 if success else 0
			if count % PRINT_EVERY == 0:
				print(f"Processed {count} Wikipedia pages. {success_count}/{count} successful.")

	backlog_cursor.close()
	backlog_db.close()

	print(f"Finished processing {success_count}/{count} Wikipedia pages")

	mycursor.close()
	mydb.close()


if __name__ == "__main__":
	main()
