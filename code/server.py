import zmq, time
import numpy as np
import copy
import sys, json, pdb, pickle, operator, collections
import helper
import predict2 as predict
from train import load_data
from itertools import izip
import inflect
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer


#Global variables
int2tags = ['shooterName','numKilled', 'numWounded', 'city']
NUM_ENTITIES = len(int2tags)
WORD_LIMIT = 1000

port = "5050"
context = zmq.Context()
socket = context.socket(zmq.REP)
socket.bind("tcp://*:%s" % port)
print "Started server on port", port

trained_model = pickle.load( open( "trained_model.p", "rb" ) )
tfidf_vectorizer = TfidfVectorizer()
inflect_engine = inflect.engine()

TFIDF_MATRICES = {}
ENTITIES = collections.defaultdict(lambda:{})
CONFIDENCES = collections.defaultdict(lambda:{})

CORRECT = collections.defaultdict(lambda:0.)
GOLD = collections.defaultdict(lambda:0.)
PRED = collections.defaultdict(lambda:0.)
evalMode = False

outFile = file(sys.argv[2], 'w')

#Environment for each episode
class Environment:

    def __init__(self, originalArticle, newArticles, goldEntities, indx):
        self.indx = indx
        self.originalArticle = originalArticle
        self.newArticles = newArticles #extra articles to process
        self.goldEntities = goldEntities 
       
        #TODO: add slots for matches in values between DB and the new article
        self.state = [0 for i in range(3 * NUM_ENTITIES + 1)]
        self.terminal = False
        
        self.bestEntities = collections.defaultdict(lambda:'') #current best entities
        self.bestConfidences = collections.defaultdict(lambda:0)

        # to keep track of extracted values from previousArticle
        if 0 in ENTITIES[self.indx]:
            self.prevEntities, self.prevConfidences = ENTITIES[self.indx][0], CONFIDENCES[self.indx][0]
        else:
            self.prevEntities, self.prevConfidences = self.extractEntitiesWithConfidences(self.originalArticle)
            ENTITIES[self.indx][0] = self.prevEntities
            CONFIDENCES[self.indx][0] = self.prevConfidences

        #calculate tf-idf similarities
        self.allArticles = [originalArticle] + self.newArticles
        self.allArticles = [' '.join(q) for q in self.allArticles]

        if self.indx in TFIDF_MATRICES:
            self.tfidf_matrix = TFIDF_MATRICES[self.indx]
        else:
            self.tfidf_matrix = tfidf_vectorizer.fit_transform(self.allArticles)
            TFIDF_MATRICES[self.indx] = self.tfidf_matrix

        #update the initial state
        self.stepNum = 0
        self.updateState(1)

        #variables for evaluation
        
        return
    
    def extractEntitiesWithConfidences(self, article):
        #article is a list of words
        joined_article = ' '.join(article)
        pred, conf_scores, conf_cnts = predict.predictWithConfidences(trained_model, joined_article, False, helper.cities)

        for i in range(len(conf_scores)):
            if conf_cnts[i] > 0:
                conf_scores[i] /= conf_cnts[i]

        return pred.split(','), conf_scores

    #find the article similarity between original and newArticle[i] (=allArticles[i+1])
    def articleSim(self, i):
        return cosine_similarity(self.tfidf_matrix[0:1], self.tfidf_matrix[i+1:i+2])[0][0]

    # update the state based on the decision from DQN
    def updateState(self, action):

        #get next article
        if self.stepNum < len(self.newArticles):
            nextArticle = self.newArticles[self.stepNum]
        else:
            nextArticle = None

        if action == 1:
            # integrate the values into the current DB state
            entities, confidences = self.prevEntities, self.prevConfidences           

            # all other tags
            for i in range(NUM_ENTITIES):
                if i==0:
                    #handle shooterName -  add to list
                    if not self.bestEntities[i]:
                        self.bestEntities[i] = entities[i]
                    elif confidences[i] > self.bestConfidences[i]:
                        self.bestEntities[i] = self.bestEntities[i] + '|' + entities[i]
                        # self.bestEntities[i] = entities[i] #use this for original replacement
                        self.bestConfidences[i] = confidences[i]                        
                else:
                    if not self.bestEntities[i] or confidences[i] > self.bestConfidences[i]:
                        self.bestEntities[i] = entities[i]
                        self.bestConfidences[i] = confidences[i]
                        # print "Changing best Entities"
                        # print "New entities", self.bestEntities

        if nextArticle:    
            if (self.stepNum+1) in ENTITIES[self.indx]:
                entities, confidences = ENTITIES[self.indx][self.stepNum+1], CONFIDENCES[self.indx][self.stepNum+1]
            else:
                entities, confidences = self.extractEntitiesWithConfidences(nextArticle)
                ENTITIES[self.indx][self.stepNum+1], CONFIDENCES[self.indx][self.stepNum+1] = entities, confidences
            assert(len(entities) == len(confidences))          
        else:
            # print "No next article"
            entities, confidences = [""]*NUM_ENTITIES, [0]*NUM_ENTITIES
            self.terminal = True

        #modify self.state appropriately        
        # print(self.bestEntities, entities)
        matches = map(self.checkEquality, self.bestEntities.values()[1:], entities[1:])
        matches.insert(0, self.checkEqualityShooter(self.bestEntities.values()[0], entities[0]))
        for i in range(NUM_ENTITIES):
            self.state[i] = self.bestConfidences[i] #DB state
            self.state[NUM_ENTITIES+i] = confidences[i]  #next article state
            self.state[2*NUM_ENTITIES+i] = int(matches[i])
            if nextArticle:
                self.state[-1] = self.articleSim(self.stepNum)
            else:
                self.state[-1] = 0

        #update state variables
        self.prevEntities = entities
        self.prevConfidences = confidences

        return

    # check if two entities are equal. Need to handle shooterName and city
    #TODO: handle the case when goldEntities does not have annotation
    def checkEquality(self, e1, e2):         
        # if gold is unknown, then dont count that
        return e2!=''  and e1.lower() == e2.lower()

    def checkEqualityShooter(self, e1, e2):
        if e2!='':
            gold = set(e2.lower().split('|'))
            pred = set(e1.lower().split('|'))
            correct = len(gold.intersection(pred))
            prec = float(correct)/len(pred)
            rec = float(correct)/len(gold)
            if prec+rec > 0:
                f1 = (2*prec*rec)/(prec+rec)
            else:
                f1 = 0.
            return f1

        return 0
        

    def calculateReward(self, oldEntities, newEntities):
        reward = sum(map(self.checkEquality, newEntities[1:], self.goldEntities[1:])) \
                - sum(map(self.checkEquality, oldEntities[1:], self.goldEntities[1:]))

        #add in shooter reward
        shooter_reward = self.checkEqualityShooter(newEntities[0], self.goldEntities[0]) \
                - self.checkEqualityShooter(oldEntities[0], self.goldEntities[0])

        # if shooter_reward != 0:
        #     print "Shooter reward", shooter_reward

        #TODO: if terminal, give some reward based on how many entities are correct?

        return reward + shooter_reward

    #evaluate the bestEntities retrieved so far for a single article
    #IMP: make sure the evaluate variables are properly re-initialized
    def evaluateArticle(self, predEntities, goldEntities):
        # print "Evaluating article", predEntities, goldEntities

        #shooterName first: only add this if gold contains a valid shooter
        if goldEntities[0]!='':
            gold = set(goldEntities[0].lower().split('|'))
            pred = set(predEntities[0].lower().split('|'))
            correct = len(gold.intersection(pred))

            # print "Gold:",goldEntities
            # print "Pred:",predEntities
            # print gold, pred, correct
            # pdb.set_trace()

            CORRECT[int2tags[0]] += correct
            GOLD[int2tags[0]] += len(gold)
            PRED[int2tags[0]] += len(pred)

        #all other tags
        for i in range(1, NUM_ENTITIES):
            GOLD[int2tags[i]] += 1
            PRED[int2tags[i]] += 1
            if predEntities[i].lower() == goldEntities[i].lower():
                CORRECT[int2tags[i]] += 1


    #take a single step in the episode
    def step(self, action):
        oldEntities = copy.copy(self.bestEntities.values())

        #update pointer to next article
        self.stepNum += 1
    

        self.updateState(action)

        newEntities = self.bestEntities.values()
        reward = self.calculateReward(oldEntities, newEntities)

        return self.state, reward, self.terminal

if __name__ == '__main__':
    env = None
    newstate, reward, terminal = None, None, None

    if len(sys.argv) > 1:
        trainFile = sys.argv[1]
    else:
        trainFile = 'train.extra'

    

    print "Running with file", trainFile    

    articles, titles, identifiers, downloaded_articles = [], [] ,[] ,[]

    #load data and process identifiers
    with open(trainFile, "rb") as inFile:
        while True:
            try:
                a, b, c, d = pickle.load(inFile)
                articles.append(a)
                titles.append(b)
                identifiers.append(c)
                downloaded_articles.append(d)                
            except:
                break

    identifiers_tmp = []  
    for e in identifiers:        
        for i in range(NUM_ENTITIES):
            if type(e[i]) == int or e[i].isdigit():            
                e[i] = int(e[i])
                e[i] = inflect_engine.number_to_words(e[i])            
        identifiers_tmp.append(e)
    identifiers = identifiers_tmp
        
    # pdb.set_trace()

    articleNum = 0
    savedArticleNum = 0

    # server loop
    while True:
        #  Wait for next request from client
        message = socket.recv()
        # print "Received request: ", message

        if message == "newGame":
            # indx = articleNum % 10
            indx = articleNum % len(articles)
            # print "INDX:", indx
            articleNum += 1
            originalArticle = articles[indx][0]
            newArticles = [q.split(' ')[:WORD_LIMIT] for q in downloaded_articles[indx]]
            goldEntities = identifiers[indx]   
            env = Environment(originalArticle, newArticles, goldEntities, indx)
            newstate, reward, terminal = env.state, 0, 'false'
        elif message == "evalStart":
            CORRECT = collections.defaultdict(lambda:0.)
            GOLD = collections.defaultdict(lambda:0.)
            PRED = collections.defaultdict(lambda:0.)
            evalMode = True
            savedArticleNum = articleNum
            articleNum = 0
            # print "##### Evaluation Started ######"
        elif message == "evalEnd":
            
            print "------------\nEvaluation Stats: (Precision, Recall, F1):"
            outFile.write("------------\nEvaluation Stats: (Precision, Recall, F1):\n")
            for tag in int2tags:
                prec = CORRECT[tag]/PRED[tag]
                rec = CORRECT[tag]/GOLD[tag]
                f1 = (2*prec*rec)/(prec+rec)
                print tag, prec, rec, f1
                outFile.write(' '.join([str(tag), str(prec), str(rec), str(f1)])+'\n')
            evalMode = False
            articleNum = savedArticleNum
            # print "##### Evaluation Ended ######"
        else:
            action = int(message)
            newstate, reward, terminal = env.step(action)        
            terminal = 'true' if terminal else 'false'
        
        #do article eval if terminal
        if evalMode and terminal == 'true':
            env.evaluateArticle(env.bestEntities.values(), env.goldEntities)


        #send message
        outMsg = 'state, reward, terminal = ' + str(newstate) + ',' + str(reward)+','+terminal
        socket.send(outMsg.replace('[', '{').replace(']', '}'))

        