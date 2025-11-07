\# Cloud-Powered Smart Notes Repository

\*\*By: Vedansh Gupta (23BIT0236) \& Kushagra Mukhija (23BIT0424)\*\*



This project is a cloud-native web application that uses AWS to create a fully searchable document repository. Users can upload PDFs and images, and the system automatically extracts all text (using OCR) and indexes it in a high-speed database, making every document searchable via a modern web interface.



\## Architecture

This application is built on a decoupled, event-driven architecture using the following AWS services:



\* \*\*Amazon S3:\*\* Used for 1) storing all original documents and 2) hosting the static HTML/JS/CSS frontend.

\* \*\*Amazon SQS:\*\* Acts as a 'to-do list' (queue) to decouple the upload process from the processing, ensuring the system is fault-tolerant and scalable.

\* \*\*Amazon EC2:\*\* A \*\*`t3.micro`\*\* instance runs two Python scripts:

&nbsp;   1.  \*\*`api\_server.py`:\*\* A Flask API server that handles search, upload, and download requests.

&nbsp;   2.  \*\*`processor.py`:\*\* A worker script that polls SQS, processes files, and saves text to DynamoDB.

\* \*\*Amazon DynamoDB:\*\* A NoSQL database that serves as our 'search index', storing all extracted text.

\* \*\*IAM, WAF, \& Security Groups:\*\* A multi-layer security approach to protect the instance and API.

\* \*\*AWS Budgets:\*\* A $10 monthly budget is in place to monitor costs.



\## How to Run This Project

This project's backend code relies on environment variables for configuration.



\### 1. Backend Setup (on EC2)

1\.  Launch an EC2 instance with an IAM Role that has `S3FullAccess`, `SQSFullAccess`, and `DynamoDBFullAccess`.

2\.  Install the required software: `sudo apt install tesseract-ocr python3-pip`

3\.  Install Python libraries: `pip3 install flask flask-cors boto3 pypdf2 pillow pytesseract`

4\.  Set the following environment variables in your EC2 environment:

&nbsp;   ```bash

&nbsp;   export S3\_BUCKET="your-s3-bucket-name"

&nbsp;   export DDB\_TABLE="Notes"

&nbsp;   export QUEUE\_URL="your-sqs-queue-url"

&nbsp;   export AWS\_REGION="eu-north-1"

&nbsp;   ```

5\.  Run the servers:

&nbsp;   ```bash

&nbsp;   python3 api\_server.py \&

&nbsp;   python3 processor.py \&

&nbsp;   ```



\### 2. Frontend Setup (on S3)

1\.  Edit the `search.html`, `upload.html`, and `dashboard.html` files.

2\.  Find the `API\_BASE\_URL` variable in the JavaScript.

3\.  Replace the placeholder IP with your EC2 instance's \*\*Public IPv4 address\*\*.

4\.  Upload all three HTML files to an S3 bucket and enable Static Website Hosting.

