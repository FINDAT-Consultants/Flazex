<?php
// Set the database connection
$db = new SQLite3('database.db');

// Get POST data from the form submission
$date = $_POST['date'];
$firstName = $_POST['firstName'];
$lastName = $_POST['lastName'];
$email = $_POST['email'];
$phoneNumber = $_POST['phoneNumber'];
$location = $_POST['location'];
$msexcel = isset($_POST['msexcel']) ? 1 : 0;
$sql = isset($_POST['sql']) ? 1 : 0;
$mspowerbi = isset($_POST['mspowerbi']) ? 1 : 0;
$tableau = isset($_POST['tableau']) ? 1 : 0;

// Insert the data into the registrations table
$query = "INSERT INTO registrations (date, firstName, lastName, email, phoneNumber, location, msexcel, sql, mspowerbi, tableau) 
          VALUES (:date, :firstName, :lastName, :email, :phoneNumber, :location, :msexcel, :sql, :mspowerbi, :tableau)";
$stmt = $db->prepare($query);
$stmt->bindValue(':date', $date, SQLITE3_TEXT);
$stmt->bindValue(':firstName', $firstName, SQLITE3_TEXT);
$stmt->bindValue(':lastName', $lastName, SQLITE3_TEXT);
$stmt->bindValue(':email', $email, SQLITE3_TEXT);
$stmt->bindValue(':phoneNumber', $phoneNumber, SQLITE3_TEXT);
$stmt->bindValue(':location', $location, SQLITE3_TEXT);
$stmt->bindValue(':msexcel', $msexcel, SQLITE3_INTEGER);
$stmt->bindValue(':sql', $sql, SQLITE3_INTEGER);
$stmt->bindValue(':mspowerbi', $mspowerbi, SQLITE3_INTEGER);
$stmt->bindValue(':tableau', $tableau, SQLITE3_INTEGER);

// Execute the query and return a response
if ($stmt->execute()) {
    echo "Registration successful!";
} else {
    echo "There was an error with your registration.";
}
?>
