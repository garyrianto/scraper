import praw
import commentDB
from argparse import ArgumentParser
from datetime import datetime
from collections import defaultdict
from sqlalchemy import create_engine
from sqlalchemy.orm import relation, sessionmaker


def _build_summary_stats(stats):    
    stats['net_karma'] = stats['pos_karma'] - stats['neg_karma']

    if stats['count'] == 0:
        stats['avg_pos_karma'] = None
        stats['avg_neg_karma'] = None
        stats['avg_net_karma'] = None
    else:
        stats['avg_pos_karma'] = float(stats['pos_karma']) / stats['count']
        stats['avg_neg_karma'] = float(stats['neg_karma']) / stats['count']
        stats['avg_net_karma'] = float(stats['net_karma']) / stats['count']


# Input: PRAW generator object for comments/submissions (posts), subreddit list
# Output: dicts with the following stats for all of reddit and for specified subreddits
#  * 'count': count of all posts
#  * 'pos_karma': total positive karma
#  * 'avg_pos_karma': average positive karma per post
#  * 'neg_karma': total negative karma
#  * 'avg_neg_karma': average negative karma per post
#  * 'net_karma': total karma
#  * 'avg_net_karma': average karma per post
#
# NOTE: we should histogram 'count' and 'count_filtered' before trusting these stats, since
# the reddit API only allows you to pull 1000 previous submissions/comments
#   --  another flaw: these stats don't reflect stats at time of posting
def user_stats(gen, subreddits):
    stats = defaultdict(lambda: defaultdict(int))
    for obj in gen:
        obj_subreddit = obj.subreddit.display_name
        if obj_subreddit in subreddits:
            stats[obj_subreddit]['count'] += 1
            stats[obj_subreddit]['pos_karma'] += obj.ups
            stats[obj_subreddit]['neg_karma'] += obj.downs
        stats['GLOBAL']['count'] += 1
        stats['GLOBAL']['pos_karma'] += obj.ups
        stats['GLOBAL']['neg_karma'] += obj.downs

    for subreddit in subreddits:
        _build_summary_stats(stats[subreddit])
    
    return stats


def load_users(r, users, subreddit_models, session):
    for username in users:
        user = r.get_redditor(username)
        comment_stats = \
            user_stats(user.get_comments(limit=None), subreddit_models)
        submission_stats = \
            user_stats(user.get_submitted(limit=None), subreddit_models)

        user_model = commentDB.User(user)
        merge_model(user_model, session)

        for subreddit in subreddit_models:
            activity_model = commentDB.UserActivity(
                user_name=user.name, 
                subreddit=subreddit_models[subreddit], 
                comment_stats=comment_stats[subreddit], 
                submission_stats=submission_stats[subreddit])
            add_model(activity_model, session)


def _max_tree_depth(comment):
    if len(comment.replies) == 0:
        return 1
    return 1 + max(_max_tree_depth(reply) for reply in comment.replies)


# Comments are ranked by 'best': 
#    http://www.redditblog.com/2009/10/reddits-new-comment-sorting-system.html
# Only storing top-level comments for now
def load_comments(comments, users, session):
    for rank, c in enumerate(comments, start=1): 
        text = c.body.encode('ascii', 'ignore')
        if text == '[deleted]':
            continue
        if c.author is not None and c.author.name not in users:
            users.add(c.author.name)
            user_model = commentDB.User(name=c.author.name)
            add_model(user_model, session)

        comment_model = commentDB.Comment(c, rank, len(c.replies), _max_tree_depth(c))
        add_model(comment_model, session)


def load_subreddit(subreddit, users, session):
    top = subreddit.get_top_from_all(limit=2000)
    for submission in top:
        if not submission.is_self:
            continue
        if submission.author is not None and submission.author.name not in users:
            users.add(submission.author.name)
            user_model = commentDB.User(name=submission.author.name)
            add_model(user_model, session)
                
        submission_model = commentDB.Submission(submission)
        add_model(submission_model, session)

        submission.replace_more_comments(limit=None, threshold=0)
        load_comments(submission.comments, users, session)


def add_model(model, session):
    try:
        session.add(model)
        session.commit()
    except:
        session.rollback()


def merge_model(model, session):
    try:
        session.merge(model)
        session.commit()
    except:
        session.rollback()


if __name__ == '__main__':
    parser = ArgumentParser(description='Scrape comments of Reddit self-posts')
    parser.add_argument('-u', '--username', type=str, default='nlu_comment_ranker',
                        help='Reddit username')
    parser.add_argument('-p', '--password', type=str, default='cardinal_cs224u',
                        help='Reddit password')
    parser.add_argument('subreddits', type=str, nargs='+',
                        help='List of subreddits to scrape')
    args = parser.parse_args()

    user_agent = ("NLU project: comment scraper " 
                  "by /u/nlu_comment_ranker (smnguyen@stanford.edu)")
    r = praw.Reddit(user_agent=user_agent)
    r.login(username=args.username, password=args.password)

    engine = create_engine('sqlite:///' + 'db.sqlite', echo=True)
    commentDB.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    subreddit_models = {}
    users = set()
    sr_global = commentDB.Subreddit(subreddit_id='GLOBAL', name='GLOBAL')
    add_model(sr_global, session)
    subreddit_models['GLOBAL'] = sr_global    

    for subreddit_name in args.subreddits:
        subreddit = r.get_subreddit(subreddit_name)
        subreddit_model = commentDB.Subreddit(subreddit)
        subreddit_models[subreddit_name] = subreddit_model
        add_model(subreddit_model, session)        
        load_subreddit(subreddit, users, session)
    load_users(r, users, subreddit_models, session)