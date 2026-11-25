"""Prepare the Pi bot database and enrich its open candidate backlog."""

import mysql.connector
import pywikibot
from database_login import (
	database_database,
	database_host,
	database_password,
	database_user,
)
from pywikibot.exceptions import NoPageError
from vector_db_functions import process_vector_data

BATCH_SIZE = 100
PRINT_EVERY = 200


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
			success = process_vector_data(
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
