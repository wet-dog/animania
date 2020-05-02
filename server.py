from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from threading import Thread
import os
from pprint import pprint

import numpy as np
import pandas as pd
from sklearn.metrics.pairwise import pairwise_distances

from jikanpy import Jikan
jikan = Jikan()

import gspread
from oauth2client.service_account import ServiceAccountCredentials

scope = ["https://spreadsheets.google.com/feeds",
         "https://www.googleapis.com/auth/spreadsheets",
         "https://www.googleapis.com/auth/drive.file",
         "https://www.googleapis.com/auth/drive"]

creds = ServiceAccountCredentials.from_json_keyfile_name("./creds.json", scope)
client = gspread.authorize(creds)
review_sheet = client.open("reviews").sheet1
user_data = client.open("animania").sheet1
user_matrix = {}
item_matrix = {}

app = Flask("animania")
CORS(app)

#====================================== GET METHODS ==================================================
@app.route('/get_user/<username>', methods=["GET"])
def get_user(username):
    cell = user_data.findall(username)
    if len(cell) == 0:
        abort(404)
    anime_list = eval(user_data.cell(cell[0].row, 2).value)
    return jsonify({'result': {'username': username, 'animes': anime_list}})

@app.route('/model_recs', methods=["GET"])
def get_model_recommendations():
    type = request.args.get('type')
    username, anime_id = "", ""

    if type is None:
        abort(400)

    if type == "user":
        username = request.args.get('username')
        if username is None:
            abort(400)
    elif type == "item":
        anime_id = request.args.get('anime_id')
        if anime_id is None:
            abort(400)
    else:
        abort(400)

    if type == "user":
        if username in user_matrix:
            similarity, username_dict = user_matrix[username]
        else:
            similarity, username_dict = build_user_matrix(username)
            user_matrix[username] = similarity, username_dict  # cache results
        key_list, val_list = list(username_dict.keys()), list(username_dict.values())
        arr_sim = similarity[username_dict[username]]

        arr_recs = np.asarray([key_list[val_list.index(i)] for i in range(len(arr_sim))], dtype=object)
        sim_inds = arr_sim.argsort()
        sorted_arr = arr_recs[sim_inds]
        top_k = sorted_arr[1:11]  # k=10, the top user is always the user itself

        top_recs = []
        for user in top_k:
            animelist = sorted(jikan.user(username=user, request='animelist')['anime'], key=score, reverse=True)[:5]
            top_recs.extend(animelist)

        return jsonify({'result': top_recs})

    else:
        if anime_id in item_matrix:
            similarity, anime_id_dict = item_matrix[anime_id]
        else:
            similarity, anime_id_dict = build_item_matrix(anime_id)
            item_matrix[anime_id] = similarity, anime_id_dict  # cache results
        key_list, val_list = list(anime_id_dict.keys()), list(anime_id_dict.values())
        arr_sim = similarity[anime_id_dict[anime_id]]

        arr_recs = np.asarray([key_list[val_list.index(i)] for i in range(len(arr_sim))], dtype=object)
        sim_inds = arr_sim.argsort()
        sorted_arr = arr_recs[sim_inds]
        top_k = sorted_arr[1:11]  # k=10, the top anime is always the anime itself

        top_recs = []
        for anime_id in top_k:
            top_recs.append(jikan.anime(anime_id))

        return jsonify({'result': top_recs})


@app.route('/completed', methods=["GET"])
def get_completed():
    req = request.get_json()
    if "username" not in req:
        abort(400)
    cell = user_data.findall(req["username"])[0]
    anime_list = user_data.cell(cell.row, 2).value

    return jsonify(anime_list)


#====================================== POST METHODS =================================================
@app.route('/add_user/<username>', methods=["POST"])
def add_user(username):
    row = [username, "{}"]
    user_data.insert_row(row, 2)

    return jsonify({'result': {'username': username, 'animes': {}}})


#====================================== DELETE METHODS ================================================
@app.route('/del_completed', methods=["DELETE"])
def del_completed():
    req = request.get_json()
    if "anime_id" not in req:
        abort(400)
    cells = review_sheet.findall(req["anime_id"])
    for cell in cells:
        username = review_sheet.row_values(cell.row)[0]
        if req.username == username:
            review_sheet.delete_row(cell.row)

    return jsonify(req)


#====================================== PATCH METHODS =================================================
@app.route('/add_completed', methods=["PATCH"])
def add_completed():
    req = request.get_json()
    for key in ["username", "anime_id", "score"]:
        if key not in req:
            abort(400)

    row = [req["username"], req["anime_id"], req["score"]]
    review_sheet.insert_row(row, 2)

    # modify animania google sheets database
    cell = user_data.findall(req["username"])[0]
    anime_list = eval(user_data.cell(cell.row, 2).value)
    anime_list[req["anime_id"]] = req["score"]
    user_data.update_cell(cell.row, 2, str(anime_list))

    return jsonify(req)


def build_item_matrix(anime_id):
    user_stats = pd.DataFrame(review_sheet.get_all_records()).sample(n=10000)
    cells = review_sheet.findall(anime_id)

    for c in cells:
        user_stats = user_stats.append({'profile': review_sheet.cell(c.row, 1).value,
                                        'anime_uid': int(review_sheet.cell(c.row, 2).value),
                                        'score': int(review_sheet.cell(c.row, 3).value)}, ignore_index=True)
    user_stats.drop_duplicates(inplace=True)
    username_dict = dict(zip([val for val in user_stats['profile'].unique()],
                             [i for i, val in enumerate(user_stats['profile'].unique())]))
    anime_id_dict = dict(zip([int(val) for val in user_stats['anime_uid'].unique()],
                             [i for i, val in enumerate(user_stats['anime_uid'].unique())]))
    num_users = user_stats.profile.nunique()
    num_animes = user_stats.anime_uid.nunique()

    train_data_matrix = np.zeros((num_users, num_animes), dtype='uint8')
    for line in user_stats.itertuples():
        train_data_matrix[username_dict[line[1]] - 1, anime_id_dict[line[2]] - 1] = line[3]

    return pairwise_distances(train_data_matrix.T, metric='cosine'), anime_id_dict


def build_user_matrix(username):
    user_stats = pd.DataFrame(review_sheet.get_all_records()).sample(n=10000)
    cells = review_sheet.findall(username)

    for c in cells:
        user_stats = user_stats.append({'profile': review_sheet.cell(c.row, 1).value,
                                        'anime_uid': int(review_sheet.cell(c.row, 2).value),
                                        'score': int(review_sheet.cell(c.row, 3).value)}, ignore_index=True)
    user_stats.drop_duplicates(inplace=True)
    username_dict = dict(zip([val for val in user_stats['profile'].unique()],
                             [i for i, val in enumerate(user_stats['profile'].unique())]))
    anime_id_dict = dict(zip([int(val) for val in user_stats['anime_uid'].unique()],
                             [i for i, val in enumerate(user_stats['anime_uid'].unique())]))
    num_users = user_stats.profile.nunique()
    num_animes = user_stats.anime_uid.nunique()

    train_data_matrix = np.zeros((num_users, num_animes), dtype='uint8')
    for line in user_stats.itertuples():
        train_data_matrix[username_dict[line[1]] - 1, anime_id_dict[line[2]] - 1] = line[3]

    return pairwise_distances(train_data_matrix, metric='cosine'), username_dict


def score(anime):
    return anime["score"]


def main():
    app.run(host='0.0.0.0', debug=False, port=os.environ.get('PORT', 80))


if __name__ == "__main__":
    # Only for debugging while developing
    Thread(target=main).start()
