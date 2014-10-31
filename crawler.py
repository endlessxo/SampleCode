#Change config.yml according to the template.

import urlparse
import urllib
import urllib2
import json
import os
import httplib

class Credentials:
	def __init__(self):
		self.username = 'x'
		self.password = '0'
		self.rest_url = 'http://qa-pilot-backend:8181/rest-services'
		self.starting_url = 'http://qa-pilot-backend:8181/rest-services/1hs/entity/JobOrder/95001?fields=*'
	
	def get_seed(self):
		return self.rest_url + '/login?username=' + self.username + '&password=' + self.password + '&version=*'
	
	def get_credentials(self):
		self.credentials = {} 
		respJson = json.loads(urllib2.urlopen(self.get_seed()).read())
		self.credentials['token'] = respJson['BhRestToken']
		self.credentials['resturl'] = respJson['restUrl']
		self.credentials['metaurl'] = self.credentials['resturl'] + 'meta' + '?BhRestToken=' + self.credentials['token']
		self.credentials['starting_url'] = self.starting_url
		return self.credentials		
 	
class Meta_Map:
	def __init__(self):
		self.crawl = Credentials()
		self.credentials = self.crawl.get_credentials()
		self.token = self.credentials['token']
		self.metaurl = self.credentials['metaurl']
		self.resturl = self.credentials['resturl']
		self.starting_url = self.credentials['starting_url']
		self.meta_map = {}

	def json_api_call(self, path, params):
		url = "{0}{1}?BhRestToken={2}&{3}".format(self.resturl, path, self.token, params)
		return json.loads(urllib2.urlopen(url).read())

	def get_entity_meta(self, entity):
		return self.json_api_call("meta/{0}".format(entity), 'fields=*')

	def is_association(self, field):
		return field['type'] == 'TO_MANY' or field['type'] == 'TON_ONE'

	def extract_associations(self, meta_json):
		return [(f['name'], f['associatedEntity']['entity']) for f in meta_json['fields'] if self.is_association(f)]

	def fill_meta_map(self):
		self.jsontext = json.loads(urllib2.urlopen(self.metaurl).read())
		for a in self.jsontext:
		    if a['entity'] is not None:
		        self.entity_name = a['entity']
		        self.associations = self.extract_associations(self.get_entity_meta(a['entity']))
		        self.meta_map[self.entity_name] = self.associations

	def get_credentials(self):
		return self.credentials

	def get_meta_map(self):
		return self.meta_map

	def get_token(self):
		return self.token

	def get_starting_url(self):
		return self.starting_url

class Crawler:
	def __init__(self):
		self.Meta_Map = Meta_Map()
		self.credentials = self.Meta_Map.get_credentials()
		self.Meta_Map.fill_meta_map()
		self.meta_map = self.Meta_Map.get_meta_map() 
		
		self.token = self.credentials['token']
		self.resturl = self.credentials['resturl']
		self.metaurl = self.credentials['metaurl']
		self.starting_url = self.credentials['starting_url']

		self.urls = []
		self.visited = []
		self.notworking = []
		self.urls.append(self.starting_url + '&BhRestToken=' + self.token)
		self.visited.append(self.starting_url + '&BhRestToken=' + self.token)

	def entity_call_tier1(self, path, entities, idNum):
		url = self.resturl + path + '/' + entities + '/' + str(idNum) + '?fields=*&BhRestToken=' + self.token
		return url

	def entity_call_tier2(self, path, entities, idNum, argv):
		url = self.resturl + path + '/' + entities + '/' + str(idNum) + '/' + argv + '?fields=*&BhRestToken=' + self.token
		return url

	def capitalize_first_letter(self, string):
		return string[0].upper() + string[1:]

	def httpcode(self, url):
		try:
			conn = urllib2.urlopen(url)
			conn.getcode()
			conn.close()
			return 0
		except urllib2.HTTPError, e:
			e.getcode()
			print url
			return e.getcode()

	def append_url(self, url):
		self.visited.append(url)
		self.urls.append(url)

	def crawl(self):
		while len(self.urls) > 0:
			print "There is " + str(len(self.urls)) + " left on the start!"
			print "The visited length is " + str(len(self.visited)) + " !"

			try:
				print self.urls[0]
				jsontext = json.loads(urllib2.urlopen(self.urls[0] + '&meta=basic').read())
			except:
				print str(self.urls[0]) + " does not work!"
				self.notworking.append(self.urls[0])
				
			category_list = []
			try:
				for data in jsontext['data']:
					try:
						try:
							if self.entity_call_tier1('entity', self.capitalize_first_letter(data), jsontext['data'][data]['id']) not in self.visited:
								self.append_url(self.entity_call_tier1('entity', self.capitalize_first_letter(data), jsontext['data'][data]['id']))
						except:
							pass
						try:
							if self.entity_call_tier1('entity', jsontext['meta']['entity'], data['id']) not in self.visited:
								self.append_url(self.entity_call_tier1('entity', jsontext['meta']['entity'], data['id']))
						except:
							pass
					except:
						pass

				#The following two for loops does most of the work. Given the map of categories, we append entity/object/id to the urls
				for entity, objects in self.meta_map.iteritems():

					if entity == jsontext['meta']['entity']:
						for i in range(0, len(objects)):
							category_list.append(objects[i][0])
				for arguments in category_list:
					if self.entity_call_tier2('entity', jsontext['meta']['entity'], jsontext['data']['id'], arguments) not in self.visited:
						self.append_url(self.entity_call_tier2('entity', jsontext['meta']['entity'], jsontext['data']['id'], arguments))

			except:
				pass

			self.urls.pop(0)

		return self.visited

crawl1 = Crawler()
print crawl1.crawl()
